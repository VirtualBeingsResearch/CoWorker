# 远程 GPU 实验工作流（AutoDL）

中文 · [English](autodl-experiment-workflow.en.md)

**目标**：用 AutoDL GPU 跑注意力筛选实验（三阶段），实现「随用随停」的完整闭环。
**前置**：autodl_sdk（/app/autodl_sdk/）已就绪，token 在 /var/lib/coworker/autodl_token.env（权限600）。

## 流程总览

```
1. 选机器   → autodl overview --region beijingDC2,westDC3   （型号+库存+价格）
2. 创建实例 → autodl create --gpu 4 --image base-image-xxx   （PRO6000 等）
3. 连接     → snapshot 拿 SSH 信息 → ssh 登录
4. 搭环境   → 安装 transformers/CUDA 依赖
5. 同步代码 → scp/rsync 上传实验脚本和数据集
6. 跑实验   → screen/tmux 守护，后台运行
7. 取结果   → scp/rsync 回传
8. 停机     → autodl power-off （停止计费）
9. 释放     → autodl release （释放资源）
```

## 1. SSH 连接

实例创建并开机后，`autodl snapshot --uuid pro-xxx` 返回：
- `ssh_command`: 如 `ssh -p 34222 root@connect.xxx.autodl.com`
- `root_password`: root 密码
- `ssh_port`: 端口

登录：
```bash
ssh -p 34222 root@connect.xxx.autodl.com
# 输入密码（不显示字符）
```

**免密配置**（推荐，避免每次输密码）：
```bash
ssh-keygen -t rsa   # 生成密钥对（本地）
# 将 ~/.ssh/id_rsa.pub 完整内容粘贴到 AutoDL 控制台 → 容器实例 → 设置密钥登录
```

**守护进程**（关键！SSH 断开不影响程序）：
```bash
screen -S exp        # 新建会话
python run_exp.py    # 跑实验
# Ctrl+A D 脱离会话；screen -r exp 恢复
# 或 tmux new -s exp / tmux attach -t exp
```

## 2. 数据同步

```bash
# 上传（本地 → 远端）
scp -P 34222 -r ./exp_data/ root@connect.xxx.autodl.com:/root/autodl-tmp/
rsync -avz -e "ssh -p 34222" ./exp_data/ root@connect.xxx.autodl.com:/root/autodl-tmp/

# 下载（远端 → 本地）
scp -P 34222 root@connect.xxx.autodl.com:/root/autodl-tmp/results.json ./
```

注意：
- `/root/autodl-tmp/` 是 AutoDL 数据盘（容量大），`/root/` 是系统盘（小）
- 大文件用 AutoDL 公网网盘（更稳）

## 3. 远端环境搭建

```bash
# AutoDL 镜像通常自带 conda/PyTorch，按需补充
pip install transformers datasets
# CUDA 版本匹配：创建时用 --cuda 参数（如 113 = CUDA 11.3+）
nvidia-smi  # 确认驱动支持
```

**推理服务部署（vLLM）**：
```bash
pip install vllm
# 加载 Qwen-2.5 3B 等模型，提供 OpenAI 兼容 API
vllm serve Qwen/Qwen2.5-3B-Instruct --port 8000
# 实验脚本通过 HTTP 调用，不直接加载模型（省显存、快）
```

## 4. 成本意识

- 按量计费：**开机即计费**，关机停计费（存储费约 0.1 元/时）
- 实验设计要「开一次机尽量跑完」：先在本地验证脚本逻辑，再上传远端跑
- 用 screen/tmux 后台跑，避免 SSH 断开导致半途而废
- 跑完立即 power-off，释放用 release（先关机再释放）

## 与 SDK 的配合

```python
from autodl_sdk import AutoDLClient, AutoDLWebClient
# 生命周期：client.create_instance / power_on / power_off / release
# 信息：web.overview / get_gpu_spec_config / query_gpu_types
# 成本：client.get_balance / estimate_instance_cost / cost_health_check
```

## 待完善

- [ ] 生成 SSH 密钥对并配置免密（需在 AutoDL 控制台操作一次）
- [ ] 实验脚本（注意力筛选）开发（见 b5361b25）
- [ ] 数据集准备（LongBench-v2-Retrieval / MuSiQue）
