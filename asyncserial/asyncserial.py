import asyncio
import os
import serial


__all__ = ["AsyncSerial"]


class AsyncSerialBase:
    def __init__(self, port=None, timeout=None, write_timeout=None, inter_byte_timeout=None,
                 **kwargs):
        if (timeout is not None
                or write_timeout is not None
                or inter_byte_timeout is not None):
            raise NotImplementedError("Use asyncio timeout features")
        self.ser = serial.serial_for_url(port, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    async def read_exactly(self, n):
        data = bytearray()
        while len(data) < n:
            remaining = n - len(data)
            data += await self.read(remaining)
        return data

    async def write_exactly(self, data):
        while data:
            res = await self.write(data)
            data = data[res:]

    async def readline(self, terminator=b"\n"):
        data = bytearray()
        while terminator not in data:
            data += await self.read_exactly(1)
        return data

if os.name != "nt":
    class AsyncSerial(AsyncSerialBase):
        def __init__(self, *args, **kwargs):
            AsyncSerialBase.__init__(self, *args, **kwargs)
            self.read_future = None
            self.write_future = None

        def fileno(self):
            return self.ser.fd

        def _read_ready(self, n):
            asyncio.get_running_loop().remove_reader(self.fileno())
            if not self.read_future.cancelled():
                try:
                    res = os.read(self.fileno(), n)
                except Exception as exc:
                    self.read_future.set_exception(exc)
                else:
                    self.read_future.set_result(res)
            self.read_future = None

        def read(self, n):
            assert self.read_future is None or self.read_future.cancelled()
            loop = asyncio.get_running_loop()
            future = asyncio.Future(loop=loop)

            if n == 0:
                future.set_result(b"")
            else:
                try:
                    res = os.read(self.fileno(), n)
                except Exception as exc:
                    future.set_exception(exc)
                else:
                    if res:
                        future.set_result(res)
                    else:
                        self.read_future = future
                        loop.add_reader(self.fileno(), self._read_ready, n)

            return future

        def _write_ready(self, data):
            asyncio.get_running_loop().remove_writer(self.fileno())
            if not self.write_future.cancelled():
                try:
                    res = os.write(self.fileno(), data)
                except Exception as exc:
                    self.write_future.set_exception(exc)
                else:
                    self.write_future.set_result(res)
            self.write_future = None

        def write(self, data):
            assert self.write_future is None or self.write_future.cancelled()
            loop = asyncio.get_running_loop()
            future = asyncio.Future(loop=loop)

            if len(data) == 0:
                future.set_result(0)
            else:
                try:
                    res = os.write(self.fileno(), data)
                except BlockingIOError:
                    self.write_future = future
                    loop.add_writer(self.fileno(), self._write_ready, data)
                except Exception as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(res)

            return future

        def close(self):
            if self.read_future is not None:
                self.read_future.get_loop().remove_reader(self.fileno())
            if self.write_future is not None:
                self.write_future.get_loop().remove_writer(self.fileno())
            self.ser.close()

else:
    import ctypes

    class HandleWrapper:
        """Wrapper for an overlapped handle which is vaguely file-object like
        (sic).

        The IOCP event loop can use these instead of socket objects.
        """
        def __init__(self, handle):
            self._handle = handle

        @property
        def handle(self):
            return self._handle

        def fileno(self):
            return self._handle

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, t, v, tb):
            pass


    class AsyncSerial(AsyncSerialBase):
        """Requires ProactorEventLoop"""
        def __init__(self, *args, **kwargs):
            AsyncSerialBase.__init__(self, *args, **kwargs)

            handle = self.fileno()

            # configure behavior similar to unix read()
            timeouts = serial.win32.COMMTIMEOUTS()
            timeouts.ReadIntervalTimeout = serial.win32.MAXDWORD
            timeouts.ReadTotalTimeoutMultiplier = serial.win32.MAXDWORD
            timeouts.ReadTotalTimeoutConstant = 0
            serial.win32.SetCommTimeouts(handle, ctypes.byref(timeouts))

            self.handle_wrapper = HandleWrapper(handle)

        def fileno(self):
            try:
                return self.ser._port_handle
            except AttributeError:
                return self.ser.hComPort

        def read(self, n):
            return asyncio.get_running_loop()._proactor.recv(self.handle_wrapper, n)

        def write(self, data):
            return asyncio.get_running_loop()._proactor.send(self.handle_wrapper, data)

        def close(self):
            self.ser.close()
