# sandbox/wheels — 离线 wheel 目录（不进 git）

`runner.Dockerfile` 构建执行镜像时**优先**用本目录的 wheel 离线安装（国内构建机出网受限时必需）；
目录为空时自动回退在线安装（版本已锁定，与下方一致），所以本目录对可构建性不是必需的。

准备离线 wheels（在能出网的机器上执行，再把 .whl 放进本目录）：

```bash
pip download -d sandbox/wheels --python-version 312 --only-binary=:all: \
  --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 \
  pyarrow==25.0.0 \
  opencv-python-headless==5.0.0.93 onnxruntime==1.28.0 pyclipper==1.4.0 shapely==2.1.2 \
  pyyaml==6.0.3 pillow==12.3.0 tqdm==4.70.0 omegaconf==2.0.0 requests==2.34.2 colorlog==6.12.0 six==1.17.0 \
  rapidocr==3.9.2 \
  python-docx==1.2.0 python-pptx==1.0.2 lxml==6.1.1 typing_extensions==4.16.0 XlsxWriter==3.2.9 \
  pypdfium2==5.13.0
```

注意：wheel 文件名必须保持原样（新版 pip 校验文件名合法性，重命名会报 "not a valid wheel filename"）。
