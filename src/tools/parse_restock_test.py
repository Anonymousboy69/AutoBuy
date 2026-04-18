import re


def parse(raw_text):
    raw_text = raw_text.strip()
    lines = [line.rstrip() for line in raw_text.splitlines()]
    blocks = [block.strip() for block in re.split(r"\n{2,}", raw_text) if block.strip()]
    if len(blocks) == 1 and len(lines) > 1:
        items = [line for line in lines if line]
    else:
        items = blocks
    return lines, blocks, items


samples = [
    "item1\nitem2\nitem3",
    "item1\n\nitem2\n\nitem3",
    "item1\nline2\n\nitem3\nline2b\n\nitem5\nline2c",
    "item1\n\nitem2\nline2\n\nitem3",
    "item1\n\nitem2\n\nitem3\n",
]
for s in samples:
    print('---')
    print(repr(s))
    lines, blocks, items = parse(s)
    print('lines', lines)
    print('blocks', blocks)
    print('items', items)
