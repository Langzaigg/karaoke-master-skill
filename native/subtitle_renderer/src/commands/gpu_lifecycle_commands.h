#pragma once

#include "../protocol/render_config.h"

#include <optional>

class QJsonObject;

namespace krok::subtitle::native::runtime {

class RenderRuntime;

}  // namespace krok::subtitle::native::runtime

namespace krok::subtitle::native::commands {

QJsonObject handleConfigureGpu(
    const QJsonObject &request,
    const std::optional<protocol::RenderConfig> &config,
    runtime::RenderRuntime *runtime
);

// Style-only differential configure: patch the style/titles/fx_sprites sections
// onto the previously parsed config (lines/rubies/vector glyph table carried
// over), then run the regular gpu_configure tail. Fails when no config exists
// or the screen section drifted (target changes go through gpu_resize_target);
// the Python caller falls back to a full configure on failure.
QJsonObject handleConfigureGpuStyle(
    const QJsonObject &request,
    std::optional<protocol::RenderConfig> *config,
    runtime::RenderRuntime *runtime
);

QJsonObject handleResizeGpuTarget(
    const QJsonObject &request,
    std::optional<protocol::RenderConfig> *config,
    runtime::RenderRuntime *runtime
);

QJsonObject handleGpuDiagnostics(
    const QJsonObject &request,
    runtime::RenderRuntime *runtime
);

QJsonObject handleCloseGpuPreview(
    const QJsonObject &request,
    runtime::RenderRuntime *runtime
);

// 泵 sidecar 线程的窗口消息队列：DComp 子窗口的鼠标转发消息在暂停/空闲
// （无 present）时积压，由 Python worker 的空闲心跳驱动本命令周期投递。
QJsonObject handlePumpNativePreview(
    const QJsonObject &request,
    runtime::RenderRuntime *runtime
);

}  // namespace krok::subtitle::native::commands
