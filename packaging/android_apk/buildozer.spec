[app]
title = Portfolio Analyzer
package.name = portfolioanalyzer
package.domain = in.rajeshkavadi
source.dir = .
source.include_exts = py,txt,csv,json
version = 0.9.2
# pure-Python requirements only (no compilation): xlsx + CAS/CG PDF + live prices
requirements = python3,setuptools,openpyxl,et_xmlfile,pypdf
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.api = 34
android.minapi = 24
android.archs = arm64-v8a
p4a.bootstrap = webview
p4a.port = 5000
android.presplash_color = #f7f8fa

[buildozer]
log_level = 2
warn_on_root = 0
