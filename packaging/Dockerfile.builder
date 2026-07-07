FROM centos:7

# yum 走代理 + 换阿里云镜像源（vault.centos.org 直连不通）
RUN echo -e "proxy=http://10.12.186.204:7897\nhttps_proxy=http://10.12.186.204:7897" >> /etc/yum.conf && \
    sed -i 's|^mirrorlist=|#mirrorlist=|g' /etc/yum.repos.d/CentOS-*.repo && \
    sed -i 's|^#baseurl=http://mirror.centos.org/centos|baseurl=https://mirrors.aliyun.com/centos|g' /etc/yum.repos.d/CentOS-*.repo

# 编译依赖
RUN yum groupinstall -y "Development Tools" && \
    yum install -y openssl-devel bzip2-devel libffi-devel zlib-devel \
        xz-devel sqlite-devel readline-devel wget && \
    yum clean all

# 编译 Python 3.8.10 with --enable-shared（源码本地提供，避免容器内下载）
ENV LD_LIBRARY_PATH=/usr/local/lib
COPY Python-3.8.10.tgz /tmp/Python.tgz
RUN cd /tmp && tar xzf Python.tgz && \
    cd Python-3.8.10 && \
    ./configure --enable-shared LDFLAGS="-Wl,-rpath /usr/local/lib" && \
    make -j$(nproc) && \
    make altinstall && \
    cd / && rm -rf /tmp/Python-3.8.10 /tmp/Python.tgz && \
    ln -sf /usr/local/bin/python3.8 /usr/local/bin/python3 && \
    ln -sf /usr/local/bin/pip3.8 /usr/local/bin/pip3

# 升级 pip（走代理 + 清华源）
RUN pip3 install --upgrade pip \
    --proxy http://10.12.186.204:7897 \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

# 验证 shared library
RUN python3 -c "import sysconfig; print('LDLIBRARY:', sysconfig.get_config_var('LDLIBRARY'))" && \
    ls -la /usr/local/lib/libpython3.8.so* && \
    python3 --version
