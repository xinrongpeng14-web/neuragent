#!/bin/bash
# 复现 P1 验证环境: 在 Docker 容器内编译 NeurDB 引擎与 nram 扩展 (含 SELIX)
# 前提: 已 clone NeurDB 并执行过 git submodule update --init (selix 子模块)
set -e
REPO=${REPO:-/home/zhanhao/neuragent/NeuralDB}
P1=$(cd "$(dirname "$0")" && pwd)
docker run -d --name neurdb-p1 --cpus=1.5 --memory=2g -v $REPO:/src:ro ubuntu:22.04 sleep infinity
docker exec neurdb-p1 bash -c 'export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq \
  build-essential flex bison libreadline-dev zlib1g-dev libicu-dev pkg-config librocksdb-dev locales'
# 1) 数据库引擎 (源码只读挂载, 树外编译)
docker exec neurdb-p1 bash -c 'mkdir -p /build/pg && cd /build/pg && \
  bash /src/dbengine/configure --prefix=/opt/neurdb --enable-debug --without-icu && make -j2 && make install'
# 2) nram 扩展: 拷贝到可写目录后编译。
#    $REPO 必须是已经打过补丁的 NeuralDB 工作区 (scripts/setup_neurdb.sh),
#    未打补丁的上游代码存在键编码缺陷, 见 P1_report.md 第 4 节。
docker exec neurdb-p1 bash -c 'cp -r /src/dbengine/nr_kernel/nr_am /build/nr_am'
docker exec neurdb-p1 bash -c 'cd /build/nr_am && make PG_CONFIG=/opt/neurdb/bin/pg_config && make PG_CONFIG=/opt/neurdb/bin/pg_config install'
# 3) 初始化并启动
docker exec neurdb-p1 bash -c 'useradd -m -s /bin/bash neurdb; mkdir -p /data && chown neurdb /data; locale-gen en_US.UTF-8; \
  su neurdb -c "/opt/neurdb/bin/initdb -D /data/pg && \
  printf \"shared_preload_libraries = '"'"'nram'"'"'\nshared_buffers = 256MB\nmax_worker_processes = 8\n\" >> /data/pg/postgresql.conf && \
  /opt/neurdb/bin/pg_ctl -D /data/pg -l /data/logfile -w start"'
# 4) 运行验证
for f in p1_verify.sql p1_perf.sql; do docker cp $P1/$f neurdb-p1:/data/$f; done
docker exec neurdb-p1 su neurdb -c "/opt/neurdb/bin/psql -d neurdb -f /data/p1_verify.sql"
docker exec neurdb-p1 su neurdb -c "/opt/neurdb/bin/psql -d neurdb -f /data/p1_perf.sql"
