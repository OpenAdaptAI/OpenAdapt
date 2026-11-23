// Check an immutable vendored snapshot. This does not claim it is the live theme.
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
const root = process.argv[2];
if (!root) throw new Error('Usage: node check-semantic-theme.mjs <vendor-directory>');
const provenance = JSON.parse(fs.readFileSync(path.join(root, 'provenance.json'), 'utf8'));
if (provenance.version !== 2 || provenance.source !== 'OpenAdaptAI/openadapt-web' || !/^[a-f0-9]{40}$/.test(provenance.commit)) throw new Error('Invalid semantic theme provenance.');
for (const [name, entry] of Object.entries(provenance.files)) {
  if (path.isAbsolute(name) || name.split(/[\\/]/).includes('..')) throw new Error('Invalid theme path.');
  const hash = crypto.createHash('sha256').update(fs.readFileSync(path.join(root, name))).digest('hex');
  if (hash !== entry.sha256) throw new Error(`Semantic theme drift: ${name}`);
}
for (const required of ['semantic-theme.css', 'semantic-theme.json', 'fonts.css', 'fonts/OFL-PublicSans.txt', 'fonts/OFL-InterTight.txt']) {
  if (!provenance.files[required]) throw new Error(`Missing semantic theme member: ${required}`);
}
const licenses = process.argv.indexOf('--licenses');
if (licenses !== -1) {
  const destination = process.argv[licenses + 1];
  if (!destination) throw new Error('--licenses requires a destination.');
  fs.mkdirSync(destination, { recursive: true });
  for (const name of ['OFL-PublicSans.txt', 'OFL-InterTight.txt']) fs.copyFileSync(path.join(root, 'fonts', name), path.join(destination, name));
}
console.log(`Semantic theme v2 matches pinned ${provenance.commit}: ${Object.keys(provenance.files).length} files.`);
