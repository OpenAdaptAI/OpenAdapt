import React from 'react'
import Home from './page'
import Screenshots from './screenshots/page'

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
        name: 'Screenshots',
        path: '/screenshots',
        component: <Screenshots />,
    },
]
