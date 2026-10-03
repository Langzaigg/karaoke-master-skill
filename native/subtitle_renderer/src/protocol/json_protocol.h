#pragma once

#include <QtCore/QJsonObject>
#include <QtCore/QString>

#include <optional>

namespace krok::subtitle::native::protocol {

// Schema 3 adds boundary dedup tables: ``fx_color_table``/``fx_paint_table``
// (burst colors & paint specs referenced by id), ``line_layout_table``
// (per-line layout snapshots), omit-default char fields, rounded glyph path
// coordinates, and the optional ``vector_glyphs_hash`` gate (table omitted
// when the content digest matches the sidecar's retained copy).
inline constexpr int kRenderIrSchema = 3;

enum class Command {
    BackendInfo,
    RenderProbe,
    GpuConfigure,
    // Style-only differential configure: reuses the parsed lines/rubies state.
    GpuConfigureStyle,
    GpuResizeTarget,
    GpuRenderFrame,
    GpuPresentFrame,
    GpuPreviewClose,
    GpuDiagnostics,
    Configure,
    RenderFrame,
    RenderFrameStats,
    RenderRangeStats,
    RenderRange,
    CancelGeneration,
    Shutdown,
    Unknown,
};

Command commandFromName(const QString &name);
QJsonObject response(bool ok, const QString &event);
QJsonObject parseErrorResponse(const QString &message);
std::optional<QJsonObject> parseRequestLine(
    const QString &line,
    QJsonObject *errorResponse
);
void writeJson(const QJsonObject &object);

}  // namespace krok::subtitle::native::protocol
