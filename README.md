# ibooks to epub

A utility to attempt to convert .ibooks files to epubs.

## Why this is hard

Most of our ibooks files have author fixed-layouts.  Every file has two CSS files:

* contentN.css: a basic fallback with Apple proprietary `-ibooks-*` properties
* contentN-paginated.css: the actual layout using Apple's proprietary `::slot()` CSS, which positions every element absolutely at exact coordiantes (e.g. `::slot(image8) {left: 30pt; top: 82pt; width: 961pt; height: 571pt; }`)

Standard epub readers don't understand `::slot()`, so all elements just stack in DOM order, look nothing like the original,
and is difficult for users to use.  Multi-page chapters make it even worse: a 3-page chapter has three `@page ::nth-instance`
blocks, each listing which elements belong to that page via `-ibooks-positioned-slots`.

