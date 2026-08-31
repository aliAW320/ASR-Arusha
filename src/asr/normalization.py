import re
import unicodedata


def normalize_persian(text):
    text = unicodedata.normalize("NFKC", str(text))
    replace_map = {
        "ي": "ی",
        "ى": "ی",
        "ئ": "ی",
        "ك": "ک",
        "ة": "ه",
        "ۀ": "ه",
        "\u200c": " ",
        "\u200d": " ",
        "\u200e": "",
        "\u200f": "",
        "\ufeff": "",
    }
    for source, target in replace_map.items():
        text = text.replace(source, target)
    text = text.translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789",
        )
    )
    text = re.sub(r"[\u064B-\u065F\u0670\u06D6-\u06ED]", "", text)
    text = "".join(
        character if character.isalnum() or character.isspace() else " "
        for character in text
    )
    return re.sub(r"\s+", " ", text).strip()
