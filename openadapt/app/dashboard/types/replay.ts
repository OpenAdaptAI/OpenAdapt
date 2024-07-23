export type Replay = {
    id: number;
    strategy_name: string;
    strategy_args: Record<string, any>;
    timestamp: number;
}


export type ReplayLog = {
    id: number;
    replay_id: number;
    lineno: number;
    filename: string;
    timestamp: number;
    key: string;
    data: Record<string, any>;
}
