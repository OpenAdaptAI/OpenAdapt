export async function get<T>(url: string, options: Partial<RequestInit> = {}): Promise<T> {
    return fetch(url, options).then((res) => res.json());
}
