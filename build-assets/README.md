# 可重复的容器依赖缓存

普通 `docker build .` 仍按 uv.lock 在线安装。
网络不稳定的机器可先在项目虚拟环境运行 `scripts/prepare_container_wheels.sh`，
再使用 `docker build --build-arg OFFLINE_BUILD=1 -t kama-core .`。

`wheels/` 与 `runtime-requirements.txt` 为本地生成数据，不提交到 Git。
运行时依赖使用锁定版本与哈希校验，缓存仅用于减少重复下载。
