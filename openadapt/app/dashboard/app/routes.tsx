import React from 'react'
import Home from './page'
import Recordings from './recordings/page'

type Route = {
    name: string
    path: string
    component: React.ReactNode
}

export const routes: Route[] = [
    {
        name: 'Home',
        path: '/',
        component: <Home />,
    },
    {
        name: 'Recordings',
        path: '/recordings',
        component: <Recordings />,
    },
]
