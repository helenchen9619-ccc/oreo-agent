# agent 代码执行镜像（M3）：预装数据分析/绘图库 + 中文字体
# 构建：docker build -f sandbox/runner.Dockerfile -t ea-sandbox-runner:local sandbox/
FROM python:3.12-slim

# 国内服务器构建：--build-arg APT_MIRROR=1 --build-arg PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/
# （本地默认空 = 官方源；trixie 的源在 deb822 格式的 debian.sources）
ARG APT_MIRROR=""
ARG PIP_INDEX=""

# 中文字体（matplotlib 中文标题/标签不乱码）
RUN if [ -n "$APT_MIRROR" ]; then sed -i 's|http://deb.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*

# formulas：纯 Python Excel 公式重算引擎（数据分析师 excel-ops 技能的交付前校验）
RUN pip install --no-cache-dir ${PIP_INDEX:+--index-url $PIP_INDEX} pandas numpy openpyxl matplotlib formulas

# ===== 以下组件优先用 sandbox/wheels/ 本地 wheel 离线安装（buildkit 容器内出网极慢/不通，pip 直连会卡死；M3.1.2 教训）；
# wheels/ 不进公开仓库（体积大），缺失时自动回退在线安装（版本与 wheels 锁定一致）。准备离线 wheels 见 sandbox/wheels/README.md。
# 注意：新版 pip（>=24.x 起逐步收紧）校验 wheel 文件名合法性，COPY 重命名为 xxx.whl 会报
# "not a valid wheel filename" —— 必须保留原始文件名（pip 26 实测踩坑）。
COPY wheels/ /tmp/wheels/

# pyarrow
RUN if ls /tmp/wheels/pyarrow-*.whl >/dev/null 2>&1; then \
      pip install --no-cache-dir /tmp/wheels/pyarrow-*.whl; \
    else \
      pip install --no-cache-dir ${PIP_INDEX:+--index-url $PIP_INDEX} pyarrow==25.0.0; \
    fi

# OCR（M3.1.4）：rapidocr 全套。
# 注意 1：rapidocr 依赖写死 opencv_python（带 GUI 库），slim 镜像缺 libGL 无法 import cv2
#   → 装 opencv-python-headless 提供 cv2 命名空间，rapidocr 本体用 --no-deps。
# 注意 2：rapidocr 3.9.2 + omegaconf 2.x 有 PosixPath 赋值 bug（main.py 里 cfg.Global.model_root_dir = PosixPath 直接炸）
#   → sed 打补丁转 str()；omegaconf 1.4.1 需 antlr4 4.9（纯 sdist 无 wheel）不可用，故保留 omegaconf 2.0.0。
RUN if ls /tmp/wheels/rapidocr-*.whl >/dev/null 2>&1; then \
      pip install --no-cache-dir --no-index --find-links=/tmp/wheels \
          opencv-python-headless onnxruntime pyclipper shapely pyyaml pillow tqdm omegaconf==2.0.0 requests colorlog six \
   && pip install --no-cache-dir --no-index --find-links=/tmp/wheels --no-deps rapidocr; \
    else \
      pip install --no-cache-dir ${PIP_INDEX:+--index-url $PIP_INDEX} \
          opencv-python-headless==5.0.0.93 onnxruntime==1.28.0 pyclipper==1.4.0 shapely==2.1.2 pyyaml==6.0.3 \
          pillow==12.3.0 tqdm==4.70.0 omegaconf==2.0.0 requests==2.34.2 colorlog==6.12.0 six==1.17.0 \
   && pip install --no-cache-dir ${PIP_INDEX:+--index-url $PIP_INDEX} --no-deps rapidocr==3.9.2; \
    fi \
 && sed -i 's|cfg.Global.model_root_dir = root_dir / "models"|cfg.Global.model_root_dir = str(root_dir / "models")|' /usr/local/lib/python3.12/site-packages/rapidocr/main.py

# 办公文书（P2' 办公文书专家）：python-docx / python-pptx（lxml 为二进制依赖）
RUN if ls /tmp/wheels/python_docx-*.whl >/dev/null 2>&1; then \
      pip install --no-cache-dir --no-index --find-links=/tmp/wheels \
          python-docx python-pptx lxml typing_extensions XlsxWriter; \
    else \
      pip install --no-cache-dir ${PIP_INDEX:+--index-url $PIP_INDEX} \
          python-docx==1.2.0 python-pptx==1.0.2 lxml==6.1.1 typing_extensions==4.16.0 XlsxWriter==3.2.9; \
    fi

# PDF 解析（2026-09，飞书云盘 PDF 落工作区后解析）：pypdfium2（Chrome 同款 PDFium 引擎，
# 中文 CID/ToUnicode 处理好，许可 Apache/BSD），单 wheel 零依赖。
# 文本抽取 + 页面渲染一体：扫描件 get_text_range 为空时 page.render().to_pil() 转图交 rapidocr。
RUN if ls /tmp/wheels/pypdfium2-*.whl >/dev/null 2>&1; then \
      pip install --no-cache-dir --no-index --find-links=/tmp/wheels pypdfium2; \
    else \
      pip install --no-cache-dir ${PIP_INDEX:+--index-url $PIP_INDEX} pypdfium2==5.13.0; \
    fi \
 && rm -rf /tmp/wheels

# matplotlib 全局默认中文字体
RUN python -c "import matplotlib, os; p=os.path.join(os.path.dirname(matplotlib.__file__),'mpl-data','matplotlibrc'); s=open(p,encoding='utf-8').read(); s=s.replace('#font.sans-serif:', 'font.sans-serif: Noto Sans CJK SC, ').replace('#axes.unicode_minus:', 'axes.unicode_minus: False #').replace('axes.unicode_minus: True', 'axes.unicode_minus: False'); open(p,'w',encoding='utf-8').write(s)"

CMD ["python"]
