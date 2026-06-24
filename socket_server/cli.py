import argparse
import sys
import logging

from .version import VERSION, REPO


def cmd_serve(args):
    """启动 TCP 服务 + 自动升级后台线程"""
    from . import setup_environment
    from .server import start_tcp_server
    from .protocol import MyTCPHandler
    from .handlers import do

    setup_environment()

    # 将 do 函数注入到 CombinedHandler
    from .server import TrackedTCPHandler
    # 动态给 MyTCPHandler 添加 on_data 实现
    class ServiceHandler(TrackedTCPHandler, MyTCPHandler):
        def on_data(self, datatype, data, **kwargs):
            return do(datatype, data, **kwargs)

    # 启动自动升级后台线程
    try:
        from .autoupgrade import start_autoupgrade_thread
        start_autoupgrade_thread()
        logger = logging.getLogger("socket_server")
        logger.info("自动升级后台线程已启动")
    except Exception as e:
        logger = logging.getLogger("socket_server")
        logger.warning(f"自动升级线程启动失败（不影响服务）: {e}")

    # 启动 TCP 服务
    start_tcp_server(args.port, ServiceHandler)


def cmd_upgrade(args):
    """立即检查 GitHub 并升级到最新版"""
    from . import setup_logging
    setup_logging()
    from .upgrader import upgrade_now
    upgrade_now()


def cmd_list(args):
    """列出 GitHub 所有 Release 的版本号 + notes"""
    from . import setup_logging
    setup_logging()
    from .upgrader import list_versions
    list_versions()


def cmd_switch(args):
    """切换到指定版本"""
    from . import setup_logging
    setup_logging()
    from .upgrader import switch_version
    switch_version(args.version)


def cmd_current(args):
    """显示当前版本号 + 版本信息"""
    from . import setup_logging
    setup_logging()
    from .upgrader import show_current
    show_current()


def cmd_start(args):
    """启动服务"""
    from . import setup_logging
    setup_logging()
    from .supervisor import service_start
    service_start()


def cmd_stop(args):
    """停止服务"""
    from . import setup_logging
    setup_logging()
    from .supervisor import service_stop
    service_stop()


def cmd_enable(args):
    """安装 systemd unit 并启用开机自启"""
    from . import setup_logging
    setup_logging()
    from .supervisor import service_enable
    service_enable()


def cmd_disable(args):
    """禁用开机自启"""
    from . import setup_logging
    setup_logging()
    from .supervisor import service_disable
    service_disable()


def cmd_autoupgrade(args):
    """开关自动升级"""
    from . import setup_logging
    setup_logging()
    from .autoupgrade import set_autoupgrade
    set_autoupgrade(args.state)


def main():
    parser = argparse.ArgumentParser(
        prog="socket_server",
        description=f"socket_server v{VERSION} — TCP 服务 + 版本管理"
    )
    sub = parser.add_subparsers(dest="command", help="可用子命令")

    # serve
    p_serve = sub.add_parser("serve", help="启动 TCP 服务")
    p_serve.add_argument("-p", "--port", type=int, default=9000, help="监听端口 (默认: 9000)")
    p_serve.set_defaults(func=cmd_serve)

    # upgrade
    p_upgrade = sub.add_parser("upgrade", help="立即检查并升级到最新版")
    p_upgrade.set_defaults(func=cmd_upgrade)

    # list
    p_list = sub.add_parser("list", help="列出 GitHub 所有 Release 版本")
    p_list.set_defaults(func=cmd_list)

    # switch
    p_switch = sub.add_parser("switch", help="切换到指定版本")
    p_switch.add_argument("version", nargs="?", default=None, help="目标版本号（不填则交互选择）")
    p_switch.set_defaults(func=cmd_switch)

    # current
    p_current = sub.add_parser("current", help="显示当前版本号")
    p_current.set_defaults(func=cmd_current)

    # start
    p_start = sub.add_parser("start", help="启动服务 (systemctl start)")
    p_start.set_defaults(func=cmd_start)

    # stop
    p_stop = sub.add_parser("stop", help="停止服务 (systemctl stop)")
    p_stop.set_defaults(func=cmd_stop)

    # enable
    p_enable = sub.add_parser("enable", help="安装 systemd unit 并启用开机自启")
    p_enable.set_defaults(func=cmd_enable)

    # disable
    p_disable = sub.add_parser("disable", help="禁用开机自启")
    p_disable.set_defaults(func=cmd_disable)

    # autoupgrade
    p_auto = sub.add_parser("autoupgrade", help="开关自动升级")
    p_auto.add_argument("state", choices=["on", "off"], help="on 或 off")
    p_auto.set_defaults(func=cmd_autoupgrade)

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        print(f"\n当前版本: v{VERSION}")
        sys.exit(0)

    args.func(args)
