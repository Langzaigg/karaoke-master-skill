#pragma once

#include "../protocol/render_config.h"

#include <QtCore/QJsonObject>

#include <optional>

namespace krok::subtitle::native::runtime {

class RenderRuntime;

}  // namespace krok::subtitle::native::runtime

namespace krok::subtitle::native::commands {

std::optional<QJsonObject> handleRenderGpuFrame(
    const QJsonObject &request,
    const std::optional<protocol::RenderConfig> &config,
    runtime::RenderRuntime *runtime
);

QJsonObject handlePresentGpuFrame(
    const QJsonObject &request,
    const std::optional<protocol::RenderConfig> &config,
    runtime::RenderRuntime *runtime
);

// G6 拆分路径：只渲染到纹理（可提前），到点后由 gpu_present_rendered 上屏。
QJsonObject handleRenderGpuFrameDirect(
    const QJsonObject &request,
    const std::optional<protocol::RenderConfig> &config,
    runtime::RenderRuntime *runtime
);

QJsonObject handlePresentRenderedGpuFrame(
    const QJsonObject &request,
    const std::optional<protocol::RenderConfig> &config,
    runtime::RenderRuntime *runtime
);

}  // namespace krok::subtitle::native::commands
