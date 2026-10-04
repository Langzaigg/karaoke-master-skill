#pragma once

#include "../render_backend.h"

#include <d3d11.h>
#include <dcomp.h>
#include <dxgi1_3.h>
#include <windows.h>
#include <wrl/client.h>

namespace krok::subtitle::native {

// pump_native_preview 命令的底层入口：只派发 DComp 子窗口自己的消息
// （见 NativePreviewSurface::pumpMessages），暂停/空闲时由 Python worker
// 心跳驱动。经 Direct2DGpuBackend::pumpNativePreviewMessages 调用。

class NativePreviewSurface {
public:
    NativePreviewSurface() = default;
    ~NativePreviewSurface();

    NativePreviewResult present(
        ID3D11Device *device,
        ID3D11DeviceContext *context,
        ID3D11Texture2D *source,
        double renderMs,
        const NativePreviewTarget &target
    );
    void pumpMessages() noexcept;
    void close() noexcept;

private:
    void ensureWindow(const NativePreviewTarget &target);
    void ensureSwapChain(ID3D11Device *device, int width, int height);
    void resizeSwapChain(int width, int height);

    HWND window_ = nullptr;
    HWND parentWindow_ = nullptr;
    int width_ = 0;
    int height_ = 0;
    Microsoft::WRL::ComPtr<IDXGISwapChain1> swapChain_;
    Microsoft::WRL::ComPtr<IDCompositionDevice> compositionDevice_;
    Microsoft::WRL::ComPtr<IDCompositionTarget> compositionTarget_;
    Microsoft::WRL::ComPtr<IDCompositionVisual> compositionVisual_;
};

}  // namespace krok::subtitle::native
