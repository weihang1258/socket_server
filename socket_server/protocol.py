import struct
import json
import time
import logging
import socketserver

from .netutils import decompress_gzip, compress_gzip

logger = logging.getLogger(__name__)


class MyTCPHandler(socketserver.BaseRequestHandler):
    """TCP 协议处理器：收包/分包/文件传输协议。

    子类需实现：
    - on_data(datatype, data, filepath) -> bytes|None
    - on_connect()
    - on_disconnect()
    """

    def setup(self):
        super().setup()
        self.filepath = "tmp"
        self.content = b""
        self.bin_recv_flag = False
        self.length = 0

    def handle(self):
        try:
            self.bufsize = 10240
            datatotal = b""
            data_len = 0
            while True:
                try:
                    data = self.request.recv(self.bufsize)
                    if not data:
                        break
                    if data_len == 0:
                        if len(data) < 4:
                            logger.error(f"首包过短: {len(data)} bytes")
                            break
                        data_len = struct.unpack("i", data[:4])[0]
                        logger.info(f"指定接收有效字节数：{data_len}")
                        datatotal += data[4:]
                        logger.info(f"本条接收长度{len(data[4:])}")
                    else:
                        datatotal += data
                        logger.info(f"本条接收长度{len(data)}")

                    if len(datatotal) < data_len:
                        logger.info(f"接收总长度{len(datatotal)}")
                        continue
                    elif len(datatotal) == data_len:
                        logger.info(f"接收总长度{len(datatotal)}")
                        data = datatotal
                        datatotal = b""
                        data_len = 0
                    else:
                        logger.error(f"收到了字节数大于发送字节数，收到{len(datatotal)},发送{data_len}")
                        datatotal = b""
                        data_len = 0
                        continue

                    logger.debug('->client: %s %s' % (len(data), data[:200].hex()))
                    # 指令传输
                    if not self.bin_recv_flag:
                        if len(data) < 4:
                            logger.error(f"指令数据过短: {len(data)} bytes")
                            continue
                        datatype = struct.unpack("i", data[:4])[0]
                        data = data[4:]
                    # 文件传输
                    else:
                        datatype = 23

                    # 文件名上传，格式：【{"filepath":filepath}】
                    if datatype in (21,):
                        logger.info("文件名上传:%s %s" % (datatype, data))
                        fileinfo = json.loads(s=data)
                        self.filepath = fileinfo.get("filepath", None)
                        self.gzip = fileinfo.get("gzip", False)
                        self.request.sendall(b"21 ok")
                        continue
                    elif datatype in (22,):  # 文件长度接收
                        logger.info("文件上传：文件长度%s %s" % (datatype, data.hex()))
                        if len(data) < 8:
                            logger.error(f"文件长度数据过短: {len(data)} bytes")
                            continue
                        self.length = struct.unpack("<Q", data)[0]
                        self.content = b""
                        self.bin_recv_flag = True  # 进入二进制文件接收状态
                        self.bufsize = 102400000
                        self.request.sendall(b"22 ok")
                        continue
                    elif datatype in (23,):  # 文件内容接收
                        logger.info("文件内容上传，类型：%s" % datatype)
                        self.content += data
                        if len(self.content) == self.length:
                            self.bufsize = 1024
                            self.bin_recv_flag = False
                            self.request.sendall(b"23 ok")
                            continue  # 文件接收完成，等待 datatype 24 写文件指令
                        elif len(self.content) > self.length:
                            self.bufsize = 1024
                            self.bin_recv_flag = False
                            raise RuntimeError(
                                f"接收字节数大于原始文件字节数：接收{len(self.content)}，原始{self.length}")
                        else:
                            continue  # 文件尚未接收完成，继续接收
                    elif datatype in (24,):  # 写文件
                        logger.info("文件上传：写文件，类型：%s" % datatype)
                        str_decompress_gzip = decompress_gzip(self.content) if self.gzip else self.content
                        if str_decompress_gzip == b"^$":
                            str_decompress_gzip = b""
                        with open(self.filepath, "wb") as f:
                            f.write(str_decompress_gzip)
                        self.content = b""
                        self.request.sendall(b"24 ok")
                        continue
                    res_do = self.on_data(datatype, data, filepath=self.filepath)
                    if res_do:
                        logger.info(f"response({len(res_do)})：%s" % res_do[:100].hex())
                        try:
                            self.request.sendall(res_do)
                        except Exception as e:
                            logger.error(f"发送错误：{e}")
                            logger.info("等待3秒，重新发送")
                            time.sleep(3)
                            try:
                                self.request.sendall(res_do)
                            except Exception as e2:
                                logger.error(f"重试发送也失败：{e2}")
                    del res_do, datatype, data
                except ConnectionResetError:
                    break
        except Exception as e:
            logger.error(f"错误：{e}")
        finally:
            self.request.close()

    def on_data(self, datatype, data, **kwargs):
        """子类需重写此方法处理业务指令"""
        return None
