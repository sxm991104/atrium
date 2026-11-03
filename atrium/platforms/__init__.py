"""平台包。

⚠️ 这里导入所有平台模块，让它们自己往 registry 登记。

    加一个平台 = 加一个文件 + 在这里导入一行。
    主程序不用改。
"""

from __future__ import annotations

# 导入即登记（见各模块里的 @register）
from atrium.platforms import dingtalk  # noqa: F401
from atrium.platforms import feishu  # noqa: F401
from atrium.platforms import registry  # noqa: F401
