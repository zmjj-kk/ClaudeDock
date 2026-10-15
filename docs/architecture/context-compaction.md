# 上下文裁剪与失败控制

默认不开启自动摘要。旧 auto_threshold 保持兼容；
新 preflight_mode=shadow 只生成上下文预算事件，不修改消息。

## 策略顺序

1. 工具结果超过单条或整轮可见正文额度后先外置。
2. enforce 预检达到 high 或硬超限时，允许 Microcompact。
3. Microcompact 只压缩带现有恢复引用的旧结果；最近普通用户轮次和完整工具链保留。
4. enabled 允许滚动摘要；最近 recent_turns_min 轮不进入摘要输入。
5. 候选须有完整六段、证据引用、至少 10% 的保守估算收益；
   根运行自动摘要还必须达到 low 水位并通过输出预留的硬限制。
6. 仍超限则 context_too_large；窗口未知则 context_window_unknown。

摘要请求也使用同一 GovernedProvider 与 current_budget。
无效、空白摘要仍计入实际返回的累计 usage。预算耗尽不能通过 compact 绕过。

## 有限恢复

仅明确 context_window_exceeded、prompt_too_long，
或供应商明确 prompt is too long 的 invalid_request_error 转为容量异常。
普通 400、认证和配额异常继续按模型错误处理。

模型容量异常最多恢复 reactive_max_retries 次，不重新执行之前的工具。
最近消息无法安全压缩时直接失败。成功压缩后由 min_steps_between 和
min_growth_ratio 抑制抖动；连续失败达到 max_consecutive_failures 后
当前压缩器停止继续发起摘要请求并发布熔断事件。

手动 session.compact 使用滚动候选、单次模型调用预算和 120 秒超时；
历史太短或无收益时返回明确失败，不强制丢弃近期消息。

限制：当前失败计数属于运行内压缩器，尚未实现跨 run 的会话级熔断状态；
超大摘要输入暂时安全拒绝，尚未实现分段摘要。子 Agent 的候选有收益校验，
最终能否发送由其 preflight 判定，低水位验收需继续完善。
