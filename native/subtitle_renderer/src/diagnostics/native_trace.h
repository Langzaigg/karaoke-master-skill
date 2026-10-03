#pragma once

// KROK_NATIVE_TRACE=1 时向 stderr 输出 sidecar 关键路径事件（worker 池任务
// 起止、follower 配置迁移、渲染请求进出）。stderr 由宿主持续回收，仅诊断
// 用，默认关闭，不进入任何产品行为。

namespace krok::subtitle::native::diagnostics {

void nativeTrace(const char *format, ...) noexcept;

}  // namespace krok::subtitle::native::diagnostics
