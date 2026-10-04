#pragma once

#include "../backends/render_backend.h"

#include <QtCore/QJsonObject>

#include <functional>
#include <memory>

namespace krok::subtitle::native::runtime {

class GpuPreviewWorkerPool {
public:
    using Work = std::function<QJsonObject(RenderBackend &, int)>;
    using Publish = std::function<void(const QJsonObject &)>;

    GpuPreviewWorkerPool(
        bool forceWarp,
        int workerCount,
        bool sharedResources,
        Publish publish
    );
    ~GpuPreviewWorkerPool();

    GpuPreviewWorkerPool(const GpuPreviewWorkerPool &) = delete;
    GpuPreviewWorkerPool &operator=(const GpuPreviewWorkerPool &) = delete;

    void pause();
    void resume(const RenderScene &scene, bool deferFollowers);
    void configure(
        const RenderScene &scene,
        bool waitForRealizations = false,
        bool deferFollowers = false
    );
    bool submit(Work work);

    // pause() 排空超时（渲染任务被并发设备使用卡死）后返回 false：
    // 池内仍有永不完成的 worker，必须 abandon() 后重建。
    bool healthy() const noexcept;
    // 废弃不健康池：detach 全部线程并整体泄漏实现对象（卡死线程仍引用
    // 其内存，析构即 UAF/terminate）。泄漏存活到进程退出，宿主随后应
    // 重启本进程。abandon 后本对象不可再使用。
    void abandon() noexcept;

    int workerCount() const noexcept;
    int readyWorkerCount() const noexcept;
    bool sharedResources() const noexcept;
    int maxOutstanding() const noexcept;
    int outstanding() const noexcept;
    // in-flight 槽被永久卡死的 worker 占据（距上次完成任务 >4s 仍拒绝提交）。
    bool submitStalled() const;
    BackendCaps capabilities() const;
    BackendDiagnostics diagnostics() const;

private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace krok::subtitle::native::runtime

