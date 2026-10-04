import subprocess


def make_tracked_corpus(root):
    root.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    paths = [root / "README.md"] + [root / f"day-{number:02d}" / "README.md" for number in (1, 7, 18)]
    for index, path in enumerate(paths):
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"# Document {index}\n\n## Chapter {index}A\n"
                        "Mars one two three four five six seven eight nine ten.\n\n"
                        f"## Chapter {index}B\n"
                        "Jupiter alpha beta gamma delta epsilon zeta eta theta.\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    return root
