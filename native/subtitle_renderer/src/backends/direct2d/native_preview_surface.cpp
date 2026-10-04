#include "native_preview_surface.h"

#include <chrono>
#include <iomanip>
#include <sstream>
#include <stdexcept>

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

LRESULT CALLBACK previewWindowProc(HWND window, UINT message, WPARAM wParam, LPARAM lParam) {
    if (message == WM_NCHITTEST) {
        return HTTRANSPARENT;
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
