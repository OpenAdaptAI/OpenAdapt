poetry install
VERSION="v$(poetry run python -m build_scripts.get_version)"
mv dist/OpenAdapt.zip dist/OpenAdapt-$VERSION.zip
mv dist/OpenAdapt.app.zip dist/OpenAdapt-$VERSION.app.zip
mv dist/OpenAdapt_Installer.exe dist/OpenAdapt_Installer-$VERSION.exe
mv dist/OpenAdapt.dmg dist/OpenAdapt-$VERSION.dmg
echo "Uploading release artifacts for version $VERSION"
gh release upload $VERSION dist/OpenAdapt-$VERSION.zip dist/OpenAdapt-$VERSION.app.zip dist/OpenAdapt_Installer-$VERSION.exe dist/OpenAdapt-$VERSION.dmg
