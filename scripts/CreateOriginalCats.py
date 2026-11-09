"""Create original vector-style cat drawings for safe public reuse."""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

root = Path("D:/QQBot/data/astrbot/plugin_data/feiyu_steward/assets")
root.mkdir(parents=True, exist_ok=True)
font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 34)
small = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 20)
captions = ["脑子：下班了 / bug：才刚开始", "代码可以重构 / 猫猫先躺一会", "我不是卡住 / 我在加载灵感", "今天的努力 / 先从伸个懒腰开始", "人类负责想办法 / 猫猫负责盯着看", "很有精神 / 只是暂时不想动"]
for i, caption in enumerate(captions):
    image = Image.new("RGB", (620, 520), (252, 246-i*2, 232+i*2))
    draw = ImageDraw.Draw(image)
    fur = (242, 179+i*3, 110+i*4)
    draw.ellipse((155, 165, 465, 420), fill=fur, outline="#433d3d", width=5)
    draw.polygon([(170,210),(170,93),(260,180)], fill=fur, outline="#433d3d", width=5)
    draw.polygon([(363,180),(448,93),(449,220)], fill=fur, outline="#433d3d", width=5)
    draw.polygon([(187,171),(187,124),(226,174)], fill="#f3a7a5")
    draw.polygon([(390,174),(431,123),(431,177)], fill="#f3a7a5")
    for x in (252, 365):
        if i % 2:
            draw.arc((x-18,230,x+18,267), 180, 350, fill="#433d3d", width=5)
        else:
            draw.ellipse((x-9,238,x+9,266), fill="#433d3d")
    draw.polygon([(297,278),(322,278),(310,292)], fill="#ad615b")
    draw.arc((285,279,310,306), 0, 100, fill="#433d3d", width=3)
    draw.arc((310,279,336,306), 80, 180, fill="#433d3d", width=3)
    for y in (281,300):
        draw.line((175,y,238,y+5),fill="#433d3d",width=3)
        draw.line((385,y+5,447,y),fill="#433d3d",width=3)
    for n,line in enumerate(caption.split(" / ")):
        draw.text((310,25+n*44),line,font=font,fill="#3b414c",anchor="mt")
    draw.text((310,475),"肥鱼 · 原创猫猫涂鸦",font=small,fill="#77746d",anchor="mt")
    path=root/f"cat-{i+1:02d}.png"
    image.save(path)
    with Image.open(path) as check:
        check.verify()
(root/"LICENSE.txt").write_text("These six cat drawings were generated from original local drawing code for this bot. No private photos or third-party meme art was used. You may reuse and publish these drawings.\n",encoding="utf-8")
print("Created and validated 6 original cat drawings.")
