#pragma once

#include <QtCore/QJsonObject>
#include <QtCore/QString>

#include <cstdint>
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
    PumpNativePreview,
    GpuRenderFrameDirect,
    GpuPresentRendered,
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

// 看门狗心跳（2026-10）：长任务（场景构建 / realization 预热 / 逐帧导出）
// 每完成一段工作就上报一条 progress 事件。GUI 端 _read_until_event 以此
// 续租等待——「忙碌但在推进」的 sidecar 永不被墙钟超时误杀；心跳停滞
// 超过租期才判死（真死锁）。同线程 250ms 节流，done==total 时强制发出。
void emitProgress(
    const QString &phase,
    std::uint64_t done,
    std::uint64_t total
);

}  // namespace krok::subtitle::native::protocol
