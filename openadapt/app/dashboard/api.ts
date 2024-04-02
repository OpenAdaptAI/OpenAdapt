export async function get<T>(url: string, options: Partial<RequestInit> = {}): Promise<T> {
    const fullURL = process.env.URL + url;
    return fetch(fullURL, options).then((res) => res.json());
}
