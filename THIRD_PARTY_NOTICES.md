# Third-party software

Roostfile's own code is under the MIT License, in `LICENSE`. It uses the packages below, which keep their own licenses. The source repository does not include them. They are installed from `requirements-ui.txt`, and a portable copy ships them in `.vendor` and `runtime`, where each package's license files sit in its `.dist-info` folder.

| Package | License | Used for |
| --- | --- | --- |
| [PySide6 and Shiboken6](https://pyside.org) (Qt for Python) | LGPL-3.0-only, GPL-2.0-only or GPL-3.0-only | The interface |
| [pypdf](https://github.com/py-pdf/pypdf) | BSD-3-Clause | Reading text from PDFs |
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2), with PDFium | BSD-3-Clause and Apache-2.0, plus the licenses of PDFium's dependencies | Drawing PDF pages |
| [PyWinRT](https://github.com/pywinrt/pywinrt) (`winrt-*`) | MIT | Reaching the text recognition built into Windows |
| [typing_extensions](https://github.com/python/typing_extensions) | PSF-2.0 | Needed by PyWinRT |
| [Python](https://www.python.org), in a portable copy | PSF-2.0 | Running the app |

A portable copy uses Qt under the LGPL. Qt's libraries stay as separate files in `.vendor\PySide6`, so they can be replaced with a compatible build. The Qt source code is available from [the Qt Project](https://code.qt.io), and the PySide source from [its repository](https://code.qt.io/cgit/pyside/pyside-setup.git/).

Text recognition is part of Windows and is not distributed with Roostfile.

## Artwork

The pictures in `assets/roo` were made for this project and are under the same MIT License. The illustration was generated with [Animagine XL 4.0](https://huggingface.co/cagliostrolab/animagine-xl-4.0) (CreativeML Open RAIL++-M), and split into layers with [See-through](https://github.com/shitagaki-lab/see-through) (Apache-2.0). Neither tool nor its model weights are included here, only the resulting pictures.

Roo is not a Live2D model. Roostfile contains no Live2D files or software.
