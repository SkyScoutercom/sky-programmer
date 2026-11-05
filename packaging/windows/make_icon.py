"""Make the .ico for the Windows build from the same icon the app uses. Needs Pillow."""
import io
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QBuffer

from sky_programmer.gui import theme

buffer = QBuffer()
buffer.open(QBuffer.OpenModeFlag.WriteOnly)
if not theme.icon_image(256).save(buffer, "PNG"):
    raise SystemExit("could not render the icon")
target = Path(__file__).parent / "sky-programmer.ico"
Image.open(io.BytesIO(bytes(buffer.data()))).save(target, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                                                 (128, 128), (256, 256)])  # fmt: skip
print(f"wrote {target}")
