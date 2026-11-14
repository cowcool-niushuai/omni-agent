---
name: Linux 生产环境健康诊断
description: 当需要排查系统负载、内存泄漏、端口占用、日志排错或进程状态时激活此技能。
tags: ops, linux, diagnostics, performance
---

# 系统健康诊断规范

## 技能说明
执行环境诊断命令时，必须恪守只读与最小影响原则：
1. **推荐安全命令**：
   - 查看负载与资源：`free -m`, `uptime`, `df -h`
   - 查看进程与端口：`ps aux --sort=-%mem | head -n 10`, `ss -tuln`
   - 查看最近系统日志：`dmesg | tail -n 20`
2. **高危拦截提醒**：
   - 严禁执行 `kill -9` 未知进程、`rm -rf /` 或重启网络服务的指令；
   - 任何涉及杀死进程或更改服务配置的操作必须触发权限审批卡片。

