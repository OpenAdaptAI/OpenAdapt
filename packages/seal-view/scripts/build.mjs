#!/usr/bin/env node
// Build @openadapt/seal-view:
//   dist/index.js, dist/styles.css  the components and checks as one ESM module
//                                   (React and @openadapt/program-graph come
//                                   from the host)
//   dist/seal-viewer.html           the offline viewer: one HTML file with
//                                   React, the program map, the Seal and its
//                                   styles inlined, and a slot for one run's
//                                   files (src/viewer)
// and copies the viewer into the launcher (openadapt/seal_view/viewer.html),
// which ships it in its wheel. The output is deterministic; `--check` rebuilds
// in memory and fails when the launcher's copy differs or the viewer is over
// its size budget.
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import * as esbuild from 'esbuild'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const repo = path.resolve(root, '..', '..')
const dist = path.join(root, 'dist')
const LAUNCHER_COPY = path.join(repo, 'openadapt', 'seal_view', 'viewer.html')
// The viewer's size budget in bytes, before compression. A change that needs
// more says so here, in the same pull request.
export const VIEWER_BUDGET = 262_144
const check = process.argv.includes('--check')

const common = {
    bundle: true,
    jsx: 'automatic',
    loader: { '.module.css': 'local-css', '.js': 'jsx' },
    // One React for everything in the viewer: this package's, also for the
    // program map's source, which has no node_modules of its own.
    alias: {
        '@openadapt/program-graph': path.join(repo, 'packages', 'program-graph', 'src'),
        react: path.join(root, 'node_modules', 'react'),
        'react-dom': path.join(root, 'node_modules', 'react-dom'),
    },
    legalComments: 'none',
    charset: 'utf8',
    logLevel: 'warning',
    write: false,
}

function out(result, ext) {
    const file = result.outputFiles.find((f) => f.path.endsWith(ext))
    if (!file) throw new Error(`the build wrote no ${ext}`)
    return file.text
}

async function library() {
    const result = await esbuild.build({
        ...common,
        entryPoints: [path.join(root, 'src', 'index.js')],
        outfile: path.join(dist, 'index.js'),
        format: 'esm',
        platform: 'neutral',
        external: ['react', 'react/jsx-runtime', 'react-dom', '@openadapt/program-graph'],
        alias: {},
    })
    return { js: out(result, '.js'), css: out(result, '.css') }
}

async function viewer() {
    const result = await esbuild.build({
        ...common,
        entryPoints: [path.join(root, 'src', 'viewer', 'viewer.jsx')],
        outfile: path.join(dist, 'viewer.js'),
        format: 'iife',
        platform: 'browser',
        target: ['es2022'],
        minify: true,
        define: { 'process.env.NODE_ENV': '"production"' },
        // Preact's React-compatible build: the same components at a sixth of React's size.
        alias: {
            ...common.alias,
            react: path.join(root, 'node_modules', 'preact', 'compat'),
            'react-dom': path.join(root, 'node_modules', 'preact', 'compat'),
            'react/jsx-runtime': path.join(root, 'node_modules', 'preact', 'jsx-runtime'),
        },
    })
    const template = fs.readFileSync(path.join(root, 'src', 'viewer', 'template.html'), 'utf8')
    // Inline: no </script or </style inside the inlined text can end its element.
    const js = out(result, '.js').replace(/<\/(script)/gi, '<\\/$1')
    const css = out(result, '.css').replace(/<\/(style)/gi, '<\\/$1')
    return template.replace('/*CSS*/', () => css).replace('/*JS*/', () => js)
}

const lib = await library()
const html = await viewer()
const size = Buffer.byteLength(html)
if (size > VIEWER_BUDGET) {
    console.error(`seal-viewer.html is ${size} bytes, over its ${VIEWER_BUDGET}-byte budget`)
    process.exit(1)
}
if (check) {
    const shipped = fs.existsSync(LAUNCHER_COPY) ? fs.readFileSync(LAUNCHER_COPY, 'utf8') : null
    if (shipped !== html) {
        console.error(`${path.relative(repo, LAUNCHER_COPY)} is not this build; run: node packages/seal-view/scripts/build.mjs`)
        process.exit(1)
    }
    console.log(`seal-viewer.html matches the launcher's copy, ${size} bytes`)
} else {
    fs.rmSync(dist, { recursive: true, force: true })
    fs.mkdirSync(dist, { recursive: true })
    fs.writeFileSync(path.join(dist, 'index.js'), lib.js)
    fs.writeFileSync(path.join(dist, 'styles.css'), lib.css)
    fs.writeFileSync(path.join(dist, 'seal-viewer.html'), html)
    fs.mkdirSync(path.dirname(LAUNCHER_COPY), { recursive: true })
    fs.writeFileSync(LAUNCHER_COPY, html)
    console.log(`built dist/ and ${path.relative(repo, LAUNCHER_COPY)}: seal-viewer.html ${size} bytes`)
}
