"""文件内容 Hash 计算工具 (synced from foxline-pro-backend-server/utils/file_hash.py)"""
import hashlib

import aiofiles


class StreamingHasher:
    """
    流式哈希计算器，支持增量更新

    用于在下载/上传过程中同步计算哈希，避免二次读取文件。

    Example:
        >>> hasher = StreamingHasher()
        >>> hasher.update(b'hello')
        >>> hasher.update(b'world')
        >>> hash_val = hasher.hexdigest()
        >>> len(hash_val)
        64
    """

    __slots__ = ('_hasher',)

    def __init__(self, algorithm: str = 'sha256'):
        """
        初始化流式哈希计算器

        Args:
            algorithm: 哈希算法名称（默认 sha256，支持 hashlib.algorithms_available 中的算法）

        Raises:
            ValueError: 不支持的哈希算法
        """
        self._hasher = hashlib.new(algorithm)

    def update(self, data: bytes) -> None:
        """
        更新哈希（支持多次调用）

        Args:
            data: 要追加的字节数据
        """
        self._hasher.update(data)

    def hexdigest(self) -> str:
        """
        获取最终哈希值

        注意：调用后 hasher 状态不变，可继续 update 并再次调用 hexdigest。

        Returns:
            十六进制哈希字符串（小写）
        """
        return self._hasher.hexdigest()


async def calculate_sha256_from_path(file_path: str, chunk_size: int = 65536) -> str:
    """
    从本地文件路径异步计算 SHA-256 哈希

    分块读取文件，内存占用恒定为 chunk_size。
    用于上传前计算 hash 以支持去重检查。

    Args:
        file_path: 本地文件路径
        chunk_size: 每次读取的块大小（字节），默认 64KB

    Returns:
        64字符十六进制 hash 字符串（小写）
    """
    hasher = hashlib.sha256()
    async with aiofiles.open(file_path, 'rb') as f:
        while chunk := await f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


def calculate_sha256_from_bytes(data: bytes) -> str:
    """
    从字节数据计算 SHA-256 哈希

    用于小文件或已在内存中的数据。

    Args:
        data: 字节数据

    Returns:
        64字符十六进制 hash 字符串（小写）
    """
    return hashlib.sha256(data).hexdigest()
