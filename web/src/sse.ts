// SSE(text/event-stream)の読み取り(本体仕様書 §15.1 の M7)。
// EventSource は POST と Authorization ヘッダーを使えないので、fetch の本文を、自分で区切って読む。

export type SseEvent = { event: string; data: unknown };

/** 届いた断片を受け取り、そろったイベントだけを返す(区切りは空行)。 */
export class SseParser {
  private buffer = "";

  feed(chunk: string): SseEvent[] {
    this.buffer += chunk.replace(/\r\n/g, "\n");
    const events: SseEvent[] = [];
    let index: number;
    while ((index = this.buffer.indexOf("\n\n")) >= 0) {
      const block = this.buffer.slice(0, index);
      this.buffer = this.buffer.slice(index + 2);
      let event = "message";
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
      }
      if (data.length === 0) continue;
      try {
        events.push({ event, data: JSON.parse(data.join("\n")) });
      } catch {
        // 形の不正なイベントは捨てる(画面を止めない)
      }
    }
    return events;
  }
}
