#include "native_preview_surface.h"

#include "../../diagnostics/native_trace.h"

#include <chrono>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <windowsx.h>

namespace krok::subtitle::native {
namespace {

constexpr wchar_t kWindowClassName[] = L"KrokSubtitleNativePreview";

std::string hresultText(const char *operation, HRESULT value) {
    std::ostringstream stream;
    stream << operation << " failed (HRESULT=0x" << std::uppercase << std::hex
           << static_cast<unsigned long>(value) << ")";
    return stream.str();
}

void checkHr(HRESULT value, const char *operation) {
    if (FAILED(value)) {
        throw BackendError(hresultText(operation, value));
    }
}

bool isClientMouseMessage(UINT message) {
    switch (message) {
        case WM_MOUSEMOVE:
        case WM_LBUTTONDOWN:
        case WM_LBUTTONUP:
        case WM_LBUTTONDBLCLK:
        case WM_RBUTTONDOWN:
        case WM_RBUTTONUP:
        case WM_RBUTTONDBLCLK:
        case WM_MBUTTONDOWN:
        case WM_MBUTTONUP:
        case WM_MBUTTONDBLCLK:
        case WM_XBUTTONDOWN:
        case WM_XBUTTONDBLCLK:
        case WM_XBUTTONUP:
            return true;
        default:
            return false;
    }
}

bool isWheelMessage(UINT message) {
    return message == WM_MOUSEWHEEL || message == WM_MOUSEHWHEEL;
}

LRESULT CALLBACK previewWindowProc(HWND window, UINT message, WPARAM wParam, LPARAM lParam) {
    // 本窗口属于 sidecar 进程，而底下的顶层窗口在主进程：HTTRANSPARENT 的
    // 穿透语义只在同线程窗口间成立，跨进程会让鼠标事件被整体丢弃——整个
    // 视频区变成输入死区，悬浮传输条收不到 hover 也收不到点击（2026-10
    // 用户实测，WindowFromPoint 直接命中本窗口）。改为把鼠标消息转发给
    // 父窗口（主进程的 Qt 按控件栈正常分发，与没有 DComp 覆盖时一致）：
    // 客户区消息的坐标换算到父窗口客户区；滚轮消息的 lParam 本就是屏幕
    // 坐标，原样转发。按键按下后主窗口会捕获鼠标，后续消息直接路由给它，
    // 不再经过本路径。
    if (isClientMouseMessage(message)) {
        if (HWND parent = GetAncestor(window, GA_PARENT)) {
            POINT point{GET_X_LPARAM(lParam), GET_Y_LPARAM(lParam)};
            MapWindowPoints(window, parent, &point, 1);
            diagnostics::nativeTrace(
                "preview forward msg=%#x src=(%d,%d) dst=(%d,%d)",
                static_cast<unsigned>(message),
                static_cast<int>(GET_X_LPARAM(lParam)),
                static_cast<int>(GET_Y_LPARAM(lParam)),
                point.x,
                point.y
            );
            PostMessageW(parent, message, wParam, MAKELPARAM(point.x, point.y));
        }
        return 0;
    }
    if (isWheelMessage(message)) {
        if (HWND parent = GetAncestor(window, GA_PARENT)) {
            PostMessageW(parent, message, wParam, lParam);
        }
        return 0;
    }
    if (message == WM_SETCURSOR) {
        // 光标形状交给父窗口（hover 手型等）。必须用带超时的有限等待：
        // 主线程可能正阻塞在自己的跨进程 SendMessageW(本窗口) 上不泵入站
        // 消息，同步 SendMessage 会互相等死（2026-10 冒烟实测，物理光标
        // 悬停在窗口上时必现）。
        if (HWND parent = GetAncestor(window, GA_PARENT)) {
            DWORD_PTR result = 0;
            SendMessageTimeoutW(
                parent,
                message,
                wParam,
                lParam,
                SMTO_ABORTIFHUNG | SMTO_BLOCK,
                50,
                &result
            );
            return static_cast<LRESULT>(TRUE);
        }
        return DefWindowProcW(window, message, wParam, lParam);
    }
    if (message == WM_ERASEBKGND) {
        return 1;
    }
    return DefWindowProcW(window, message, wParam, lParam);
}

ATOM ensureWindowClass() {
    static const ATOM atom = [] {
        WNDCLASSEXW windowClass{};
        windowClass.cbSize = sizeof(windowClass);
        windowClass.lpfnWndProc = previewWindowProc;
        windowClass.hInstance = GetModuleHandleW(nullptr);
        windowClass.lpszClassName = kWindowClassName;
        windowClass.hCursor = LoadCursorW(nullptr, IDC_ARROW);
        const ATOM registered = RegisterClassExW(&windowClass);
        if (registered == 0 && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) {
            throw BackendError("RegisterClassExW(native preview) failed");
        }
        return registered;
    }();
    return atom;
}

void pumpWindowMessages() noexcept {
    MSG message{};
    while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
}

}  // namespace

NativePreviewSurface::~NativePreviewSurface() {
    close();
}

void NativePreviewSurface::ensureWindow(const NativePreviewTarget &target) {
    auto *parent = reinterpret_cast<HWND>(target.parentWindow);
    if (parent == nullptr || !IsWindow(parent)) {
        throw BackendError("native preview parent HWND is invalid");
    }
    if (target.width <= 0 || target.height <= 0) {
        throw BackendError("native preview dimensions must be positive");
    }
    if (window_ != nullptr && parentWindow_ != parent) {
        close();
    }
    if (window_ == nullptr) {
        ensureWindowClass();
        // 不用 WS_EX_NOREDIRECTIONBITMAP：它省掉一张重定向表面（省显存），
        // 但 BitBlt/PrintWindow 等截图 API 依赖重定向表面——没有它 PrtScn
        // 和第三方截图工具会失效甚至卡死（2026-10 用户实测）。DComp 直画
        // 不需要此标志；保留重定向表面的 ~15MB 开销换截图兼容性。
        // WS_EX_TRANSPARENT（非 layered 时仅影响绘制序/合成路径，不参与
        // 命中测试——穿透靠窗口过程转发）：2026-10 实测缺少它时，父窗口里
        // Qt Multimedia 的视频呈现层会被 DWM 长时间冻结（解码照常出帧但
        // 屏幕不更新，A/B 复现 G5 9/10 vs G6 4/10）。
        window_ = CreateWindowExW(
            WS_EX_NOACTIVATE | WS_EX_TRANSPARENT,
            kWindowClassName,
            L"",
            WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS,
            target.x,
            target.y,
            target.width,
            target.height,
            parent,
            nullptr,
            GetModuleHandleW(nullptr),
            nullptr
        );
        if (window_ == nullptr) {
            throw BackendError("CreateWindowExW(native preview) failed");
        }
        parentWindow_ = parent;
    }
    if (!SetWindowPos(
            window_, HWND_TOP, target.x, target.y, target.width, target.height,
            SWP_NOACTIVATE | SWP_SHOWWINDOW)) {
        throw BackendError("SetWindowPos(native preview) failed");
    }
    pumpWindowMessages();
}

void NativePreviewSurface::ensureSwapChain(ID3D11Device *device, int width, int height) {
    if (swapChain_ != nullptr) {
        if (width_ != width || height_ != height) {
            resizeSwapChain(width, height);
        }
        return;
    }
    Microsoft::WRL::ComPtr<IDXGIDevice> dxgiDevice;
    checkHr(device->QueryInterface(IID_PPV_ARGS(dxgiDevice.ReleaseAndGetAddressOf())), "Query IDXGIDevice(native preview)");
    Microsoft::WRL::ComPtr<IDXGIAdapter> adapter;
    checkHr(dxgiDevice->GetAdapter(adapter.ReleaseAndGetAddressOf()), "IDXGIDevice::GetAdapter(native preview)");
    Microsoft::WRL::ComPtr<IDXGIFactory2> factory;
    checkHr(adapter->GetParent(IID_PPV_ARGS(factory.ReleaseAndGetAddressOf())), "IDXGIAdapter::GetParent(native preview)");

    DXGI_SWAP_CHAIN_DESC1 description{};
    description.Width = static_cast<UINT>(width);
    description.Height = static_cast<UINT>(height);
    description.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    description.Stereo = FALSE;
    description.SampleDesc.Count = 1;
    description.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    description.BufferCount = 2;
    description.Scaling = DXGI_SCALING_STRETCH;
    description.SwapEffect = DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL;
    description.AlphaMode = DXGI_ALPHA_MODE_PREMULTIPLIED;
    checkHr(
        factory->CreateSwapChainForComposition(
            device,
            &description,
            nullptr,
            swapChain_.ReleaseAndGetAddressOf()
        ),
        "IDXGIFactory2::CreateSwapChainForComposition"
    );

    checkHr(
        DCompositionCreateDevice(
            dxgiDevice.Get(),
            __uuidof(IDCompositionDevice),
            reinterpret_cast<void **>(compositionDevice_.ReleaseAndGetAddressOf())
        ),
        "DCompositionCreateDevice"
    );
    checkHr(
        compositionDevice_->CreateTargetForHwnd(
            window_, TRUE, compositionTarget_.ReleaseAndGetAddressOf()
        ),
        "IDCompositionDevice::CreateTargetForHwnd"
    );
    checkHr(
        compositionDevice_->CreateVisual(compositionVisual_.ReleaseAndGetAddressOf()),
        "IDCompositionDevice::CreateVisual"
    );
    checkHr(compositionVisual_->SetContent(swapChain_.Get()), "IDCompositionVisual::SetContent");
    checkHr(compositionTarget_->SetRoot(compositionVisual_.Get()), "IDCompositionTarget::SetRoot");
    checkHr(compositionDevice_->Commit(), "IDCompositionDevice::Commit");
    width_ = width;
    height_ = height;
}

void NativePreviewSurface::resizeSwapChain(int width, int height) {
    checkHr(
        swapChain_->ResizeBuffers(
            2,
            static_cast<UINT>(width),
            static_cast<UINT>(height),
            DXGI_FORMAT_B8G8R8A8_UNORM,
            0
        ),
        "IDXGISwapChain1::ResizeBuffers(native preview)"
    );
    width_ = width;
    height_ = height;
}

NativePreviewResult NativePreviewSurface::present(
    ID3D11Device *device,
    ID3D11DeviceContext *context,
    ID3D11Texture2D *source,
    double renderMs,
    const NativePreviewTarget &target
) {
    if (device == nullptr || context == nullptr || source == nullptr) {
        throw BackendError("native preview received an empty D3D resource");
    }
    D3D11_TEXTURE2D_DESC sourceDescription{};
    source->GetDesc(&sourceDescription);
    // 子窗口矩形可以小于渲染纹理（场景映射矩形被视口裁剪），但拷贝源
    // 区域必须完整落在纹理内，否则看到的是越界垃圾。
    if (target.srcX < 0 || target.srcY < 0
        || target.width <= 0 || target.height <= 0
        || static_cast<UINT>(target.srcX + target.width) > sourceDescription.Width
        || static_cast<UINT>(target.srcY + target.height) > sourceDescription.Height) {
        throw BackendError("native preview source region exceeds the GPU texture");
    }
    ensureWindow(target);
    ensureSwapChain(device, target.width, target.height);

    const auto presentStart = std::chrono::steady_clock::now();
    Microsoft::WRL::ComPtr<ID3D11Texture2D> backBuffer;
    checkHr(
        swapChain_->GetBuffer(0, IID_PPV_ARGS(backBuffer.ReleaseAndGetAddressOf())),
        "IDXGISwapChain1::GetBuffer(native preview)"
    );
    D3D11_BOX sourceBox;
    sourceBox.left = static_cast<UINT>(target.srcX);
    sourceBox.top = static_cast<UINT>(target.srcY);
    sourceBox.front = 0;
    sourceBox.right = static_cast<UINT>(target.srcX + target.width);
    sourceBox.bottom = static_cast<UINT>(target.srcY + target.height);
    sourceBox.back = 1;
    // 拷贝区域与 back buffer 同尺寸：flip 模型 swap chain 要求每帧覆盖
    // 整个 back buffer，1:1 无缩放拷贝正好满足，且不会有重采样模糊。
    context->CopySubresourceRegion(
        backBuffer.Get(),
        0,
        0,
        0,
        0,
        source,
        0,
        &sourceBox
    );
    checkHr(swapChain_->Present(0, 0), "IDXGISwapChain1::Present(native preview)");
    pumpWindowMessages();
    const auto presentEnd = std::chrono::steady_clock::now();

    NativePreviewResult result;
    result.renderMs = renderMs;
    result.presentMs = std::chrono::duration<double, std::milli>(
        presentEnd - presentStart
    ).count();
    result.childWindow = reinterpret_cast<std::uintptr_t>(window_);
    return result;
}

void NativePreviewSurface::pumpMessages() noexcept {
    // 无限制泵（与 present 内部的 pumpWindowMessages 同语义）：限定 HWND
    // 的 PeekMessage 不投递挂起的跨线程 SENT 消息，SendMessage 进来的
    // 鼠标事件将永远不被派发（2026-10 冒烟实测主线程卡死在 SendMessage）。
    // present 每帧做同样的无限制派发，语义保持一致。
    pumpWindowMessages();
}

void NativePreviewSurface::close() noexcept {
    if (compositionVisual_ != nullptr) {
        compositionVisual_->SetContent(nullptr);
    }
    if (compositionTarget_ != nullptr) {
        compositionTarget_->SetRoot(nullptr);
    }
    if (compositionDevice_ != nullptr) {
        compositionDevice_->Commit();
    }
    compositionVisual_.Reset();
    compositionTarget_.Reset();
    compositionDevice_.Reset();
    swapChain_.Reset();
    width_ = 0;
    height_ = 0;
    if (window_ != nullptr) {
        pumpWindowMessages();
        DestroyWindow(window_);
        window_ = nullptr;
    }
    parentWindow_ = nullptr;
}

}  // namespace krok::subtitle::native
