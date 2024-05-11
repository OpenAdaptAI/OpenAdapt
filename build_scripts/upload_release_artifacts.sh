poetry install
VERSION="v$(poetry run python -m build_scripts.get_version)"
mv dist/OpenAdapt.zip dist/OpenAdapt-$VERSION.zip
mv dist/OpenAdapt.app.zip dist/OpenAdapt-$VERSION.app.zip
echo "Uploading release artifacts for version $VERSION"
gh release upload $VERSION dist/OpenAdapt-$VERSION.zip dist/OpenAdapt-$VERSION.app.zip
