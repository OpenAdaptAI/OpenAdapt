export const timeStampToDateString = (timeStamp: number) => {
    const date = new Date(timeStamp * 1000);
    return `${date.toLocaleDateString()} ${date.toLocaleTimeString()}`;
}
