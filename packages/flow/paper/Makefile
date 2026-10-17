SHELL := /bin/sh
BUILD_DIR := build

.PHONY: all check workshop arxiv arxiv-workshop clean

# `all` gate-checks every headline constant against the benchmark artifacts,
# then builds BOTH the full technical report (main.tex) and the workshop
# condensation (workshop/main.tex). check_artifacts.py binds both PDFs.
all: check
	mkdir -p $(BUILD_DIR)
	latexmk -pdf -interaction=nonstopmode -halt-on-error \
		-output-directory=$(BUILD_DIR) main.tex
	$(MAKE) workshop

# Workshop condensation. workshop/references.bib is a byte-identical regular-file
# copy of ../references.bib (NOT a symlink -- a symlink member breaks the sdist
# packaging gate); the build runs from the workshop directory so bibtex resolves
# the local copy unambiguously. check_artifacts.py asserts the two stay identical.
workshop:
	mkdir -p workshop/$(BUILD_DIR)
	cd workshop && latexmk -pdf -interaction=nonstopmode -halt-on-error \
		-output-directory=$(BUILD_DIR) main.tex

check:
	python3 check_artifacts.py

# arXiv submission tarballs. The script gate-checks constants, does a clean
# build to produce the .bbl (arXiv does not run bibtex), stages only the files
# the document \inputs, re-compiles the staged tree standalone, and refuses to
# emit a tarball with any undefined citation or reference.
arxiv:
	sh make_arxiv_tarball.sh main

arxiv-workshop:
	sh make_arxiv_tarball.sh workshop

clean:
	latexmk -C -output-directory=$(BUILD_DIR) main.tex 2>/dev/null || true
	cd workshop && latexmk -C -output-directory=$(BUILD_DIR) main.tex 2>/dev/null || true
	rm -rf $(BUILD_DIR) workshop/$(BUILD_DIR) dist
