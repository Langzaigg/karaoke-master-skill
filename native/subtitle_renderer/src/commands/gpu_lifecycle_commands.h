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

}  // namespace krok::subtitle::native::commands
