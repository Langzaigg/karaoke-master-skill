#pragma once

#include "render_config.h"

#include <QtCore/QJsonObject>
#include <QtCore/QJsonValue>
#include <QtCore/QString>

#include <optional>

namespace krok::subtitle::native::protocol {

std::optional<RenderConfig> parseRenderConfig(
    const QJsonObject &ir,
    QString *error
);
ResolvedStyle resolvedStyleFromTitle(
    const ResolvedStyle &base,
    const QJsonObject &title
);
QString resolvedStyleKey(int singerId, const QString &roleLabel);
const ResolvedStyle &resolvedStyleForLine(
    const RenderConfig &cfg,
    const TimingLine &line
);
const ResolvedStyle &resolvedStyleForCharacter(
    const RenderConfig &cfg,
    const TimingLine &line,
    const TimingChar &ch
);
// ``roleLabel`` 的方案叠加到 ``lineStyle`` 上（无方案时原样返回）；供
// 指示灯/音量柱 ``role`` 外观档在场景投影里解析固定装饰源。
ResolvedStyle resolvedStyleForRole(
    const RenderConfig &cfg,
    const ResolvedStyle &lineStyle,
    const QString &roleLabel
);
// 标题导唱符投影复用歌词字符的解析器：位图读 bitmap_guide IR 键名，
// 矢量读内嵌 path_commands 轮廓（render_ir._title_guide_to_ir 同构）。
std::optional<krok::subtitle::native::VectorGlyph> parseVectorGlyph(
    const QJsonValue &value
);
std::optional<krok::subtitle::native::BitmapGuide> parseBitmapGuide(
    const QJsonValue &value
);

}  // namespace krok::subtitle::native::protocol

