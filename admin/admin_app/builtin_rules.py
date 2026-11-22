"""默认审核词。和审核工具里的内置词一致，第一次给公司建规则表时写入。"""

_SPEECH_PRICE = (
    "到手",
    "活动价",
    "X折",
    "折扣",
    "立减",
    "直降",
    "送",
    "降价",
    "涨价",
    "清仓",
    "甩卖",
    "大促",
    "促销",
    "降价元",
    "块",
    "钱",
    "毛钱",
    "人民币",
    "RMB",
    "价格",
    "X.X贵",
    "便宜",
    "白菜价",
    "地板价",
    "两位数",
    "三位数",
    "几十块",
    "几块钱",
    "百来块",
    "小几百",
    "一杯奶茶钱",
    "一顿饭钱",
    "个位数",
    "一顿外卖钱",
    "一支口红钱",
    "一顿火锅钱",
    "一瓶矿泉水钱",
    "买贵包赔",
    "退",
    "包邮",
    "抢",
    "活动",
    "秒杀",
)

_SPEECH_SPEC = (
    "个",
    "只",
    "件",
    "片",
    "袋",
    "盒",
    "瓶",
    "包",
    "箱",
    "罐",
    "条",
    "根",
    "双",
    "套",
    "组",
    "份",
    "颗",
    "粒",
    "张",
    "台",
    "架",
    "拍1发X",
    "买一送X",
    "cm",
    "厘米",
    "mm",
    "毫米",
    "m",
    "米",
    "寸",
    "英寸",
    "尺",
    "码",
    "公分",
    "纯棉",
    "全棉",
    "桑蚕丝",
    "涤纶",
    "牛津布",
    "304不锈钢",
    "食品级",
    "医用级",
    "母婴级",
    "陶瓷",
    "硅胶",
    "实木",
    "合成革",
    "亚克力",
)

_SPEECH_BRAND = ("厂家", "官方", "正品", "同款", "抖音", "工厂")

_PICTURE_BRAND = (
    "logo",
    "商标",
    "标",
    "标识",
    "品牌标",
    "铭牌",
    "标签",
    "水印",
    "注册商标",
    "®",
    "™",
    "品牌slogan",
    "包装标",
    "口播违规字幕",
)

DEFAULT_CATEGORIES = ["价格", "规格", "品牌", "夸大宣传"]


def default_phrases() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for text in _SPEECH_PRICE:
        items.append({"text": text, "category": "价格", "source": "口播"})
    for text in _SPEECH_SPEC:
        items.append({"text": text, "category": "规格", "source": "口播"})
    for text in _SPEECH_BRAND:
        items.append({"text": text, "category": "品牌", "source": "口播"})
    for text in _PICTURE_BRAND:
        items.append({"text": text, "category": "品牌", "source": "画面"})
    return items
