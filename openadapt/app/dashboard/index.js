// run `npm run dev` as a child process

const { spawn } = require('child_process')

const net = require('net')
const checkPort = (port) => {
    return new Promise((resolve, reject) => {
        const server = net.createServer()
        server.unref()
        server.on('error', reject)
        server.listen(port, () => {
            server.close(() => {
                resolve(port)
            })
        })
    })
}

// check if both ports are not being used
const { DASHBOARD_CLIENT_PORT, DASHBOARD_SERVER_PORT } = process.env
Promise.all([checkPort(DASHBOARD_CLIENT_PORT), checkPort(DASHBOARD_SERVER_PORT)])
.then(([clientPort, serverPort]) => {
    if (clientPort !== DASHBOARD_CLIENT_PORT) {
        console.error(`Port ${DASHBOARD_CLIENT_PORT} is already in use`)
        process.exit(1)
    }
    if (serverPort !== DASHBOARD_SERVER_PORT) {
        console.error(`Port ${DASHBOARD_SERVER_PORT} is already in use`)
        process.exit(1)
    }
    spawnChildProcess()
})

function spawnChildProcess() {
    // Spawn child process to run `npm run dev`
    const childProcess = spawn('npm', ['run', 'dev'], { stdio: 'inherit' })
    // wait for 1 second
    setTimeout(() => {
        import('open').then(({ default: open }) => {
            open(`http://localhost:${DASHBOARD_CLIENT_PORT}`)
        })
    }, 3000)

    // Handle SIGTERM signal
    process.on('SIGTERM', () => {
        childProcess.kill('SIGTERM')
    })

    // Listen for child process error event
    childProcess.on('error', (err) => {
        console.error('Error executing child process:', err)
    })
}
