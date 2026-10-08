# app translations

本目录存放 app 自己的 Qt 翻译文件：

- `app.zh_CN.ts` — 源（简体中文）。`pylupdate6` 扫描源码生成。
- `app.zh_CN.qm` — 编译产物。`lrelease` 由 `.ts` 编译而成。
- 未来 `app.en_US.ts` / `app.ja_JP.ts` 同理。

注意：源字符串本身就是简体中文。`zh_CN` 的 `.qm` 即使为空或缺失，运行时 `tr()`
会回落到源字符串，行为正确——这就是为什么本次只搭骨架不提供 zh_CN.qm 也 OK。

构建流程（之后做 EN/JA 时使用）：

```bash
# 1. 扫描源码生成/更新 .ts
pylupdate6 src/strange_uta_game/frontend/**/*.py \
    -ts src/strange_uta_game/frontend/localization/translations/app.zh_CN.ts

# 2. 翻译完成后编译为 .qm
lrelease src/strange_uta_game/frontend/localization/translations/app.zh_CN.ts
```

qfluentwidgets 自己的翻译由其包内 `:/qfluentwidgets/i18n/qfluentwidgets.<locale>.qm`
提供，不需要我们维护——见 `manager.py::_build_fluent_translator`。

## 推荐流程（一键）

```bash
python scripts/update_translations.py
```

该脚本封装完整流水线：`pylupdate6` 扫描刷新 `.ts`（location 全自动）→
恢复被误标 `vanished` 的活条目 → `lrelease` 编译 `.qm`。

**为什么需要恢复步骤**：pylupdate6 只能静态识别 `self.tr("字面量")` 直呼；
本项目存在两类它看不见的调用形态——`_tr = self.tr` 别名枚举（按类型删除/
补全时间戳对话框）和「字典 + 动态 `tr(变量)`」分发。这些字符串会被整批
误标 `type="vanished"`，而 lrelease 不编译 vanished 条目 → 运行时丢翻译。
脚本按「source 字符串仍以 tr 族字面量存在于 frontend 源码」判据恢复并
回填 location。真正从源码消失的字符串保持 vanished（正确清理语义）。
