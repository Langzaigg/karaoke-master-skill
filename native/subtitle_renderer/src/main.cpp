#include <QtCore/QByteArray>
#include <QtCore/QCoreApplication>
#include <QtCore/QJsonObject>
#include <QtCore/QObject>
#include <QtCore/QTimer>
#include <QtWidgets/QApplication>

#include <atomic>
#include <iostream>
#include <string>
#include <thread>

#include "commands/command_router.h"
#include "protocol/json_protocol.h"

namespace {

using krok::subtitle::native::protocol::kRenderIrSchema;
using krok::subtitle::native::protocol::emitProgress;
using krok::subtitle::native::protocol::parseRequestLine;
using krok::subtitle::native::protocol::response;
using krok::subtitle::native::protocol::writeJson;
using krok::subtitle::native::commands::CommandRouter;

// 命令执行必须留在主线程：GPU 设备、DComp 目标与本线程的子窗口队列
// 都绑定在它身上。stdin 读是纯 I/O，挪到专用读线程，逐行经事件循环
// 派回主线程。此前主线程阻塞在 readLine 上不是消息循环线程——G6 直画
// 子窗口的真实鼠标消息永远进不了它的队列（输入黑洞），悬浮控件失效的
// 根源（2026-10 实测：SendMessage 合成消息直达窗口过程所以能转发，
// SendInput 真实消息投递为零）。
void processLine(CommandRouter &router, const QString &line) {
    QJsonObject parseError;
    const auto request = parseRequestLine(line, &parseError);
    if (!request.has_value()) {
        writeJson(parseError);
        return;
    }
    const auto result = router.dispatch(*request);
    if (result.response.has_value()) {
        writeJson(*result.response);
    }
    if (result.shutdownRequested) {
        QCoreApplication::quit();
    }
}

}  // namespace

int main(int argc, char **argv) {
#if !defined(Q_OS_WIN)
    qputenv("QT_QPA_PLATFORM", qgetenv("QT_QPA_PLATFORM").isEmpty() ? QByteArray("offscreen") : qgetenv("QT_QPA_PLATFORM"));
#endif
    QApplication app(argc, argv);

    QJsonObject ready = response(true, QStringLiteral("ready"));
    ready.insert(QStringLiteral("schema"), kRenderIrSchema);
    ready.insert(QStringLiteral("gpu_protocol"), 1);
    ready.insert(QStringLiteral("native_preview_protocol"), 1);
    ready.insert(QStringLiteral("qt"), QString::fromLatin1(qVersion()));
    writeJson(ready);

    CommandRouter router;
    QObject commandContext;
    commandContext.moveToThread(app.thread());

    // 看门狗存活心跳（2026-10）：sidecar 空闲时（暂停/播完/无任务、主
    // 线程回事件循环等待命令）由定时器每秒喂狗；忙碌时由任务内的
    // protocol::emitProgress 逐段喂（场景构建/realization 预热/逐帧导
    // 出——那时主线程阻塞在 dispatch 里，本定时器发不出来，两层互补）。
    // 主线程与所有工作线程同时停走（真死锁）时两层都停，GUI 的租期到
    // 期即判死。
    QTimer idleHeartbeat;
    QObject::connect(
        &idleHeartbeat, &QTimer::timeout,
        &commandContext, []() {
            krok::subtitle::native::protocol::emitProgress(
                QStringLiteral("idle"), 0, 0
            );
        }
    );
    idleHeartbeat.start(1000);

    std::atomic<bool> inputOpen{true};
    std::thread reader([&router, &commandContext, &inputOpen]() {
        std::string line;
        while (std::getline(std::cin, line)) {
            if (line.empty()) {
                continue;
            }
            const QString qtLine = QString::fromStdString(line).trimmed();
            if (qtLine.isEmpty()) {
                continue;
            }
            QMetaObject::invokeMethod(
                &commandContext,
                [&router, qtLine]() { processLine(router, qtLine); },
                Qt::QueuedConnection
            );
        }
        inputOpen.store(false);
        // stdin 关闭（宿主退出）：没有更多命令，退出事件循环。
        QMetaObject::invokeMethod(&commandContext, &QCoreApplication::quit,
                                  Qt::QueuedConnection);
    });

    const int exitCode = app.exec();
    reader.join();
    router.shutdown();
    return exitCode;
}
