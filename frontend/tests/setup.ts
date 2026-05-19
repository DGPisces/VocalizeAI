import "@testing-library/jest-dom/vitest";

export class MockWebSocket extends EventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: MockWebSocket[] = [];

  readonly CONNECTING = 0;
  readonly OPEN = 1;
  readonly CLOSING = 2;
  readonly CLOSED = 3;
  readyState = MockWebSocket.OPEN;
  bufferedAmount = 0;
  binaryType: BinaryType = "blob";
  sent: Array<string | ArrayBuffer | Blob | ArrayBufferView> = [];
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;

  constructor(readonly url: string) {
    super();
    MockWebSocket.instances.push(this);
    queueMicrotask(() => this.onopen?.(new Event("open")));
  }

  send(data: string | ArrayBuffer | Blob | ArrayBufferView) {
    this.sent.push(data);
  }

  close() {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({ type: "close" } as CloseEvent);
  }

  emitMessage(data: string | ArrayBuffer) {
    this.onmessage?.(new MessageEvent("message", { data }));
  }

  emitClose() {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.({ type: "close" } as CloseEvent);
  }

  emitError() {
    this.onerror?.(new Event("error"));
  }
}

Object.defineProperty(globalThis, "WebSocket", {
  configurable: true,
  value: MockWebSocket
});

const localStorageStore = new Map<string, string>();
const localStorageMock: Storage = {
  get length() {
    return localStorageStore.size;
  },
  clear() {
    localStorageStore.clear();
  },
  getItem(key: string) {
    return localStorageStore.get(key) ?? null;
  },
  key(index: number) {
    return Array.from(localStorageStore.keys())[index] ?? null;
  },
  removeItem(key: string) {
    localStorageStore.delete(key);
  },
  setItem(key: string, value: string) {
    localStorageStore.set(key, value);
  }
};

Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  value: localStorageMock
});

Object.defineProperty(window, "localStorage", {
  configurable: true,
  value: localStorageMock
});

// jsdom does not implement scrollIntoView; provide a no-op so components
// calling it don't throw. Individual tests may override on the prototype.
Element.prototype.scrollIntoView = Element.prototype.scrollIntoView ?? function () {};

beforeEach(() => {
  MockWebSocket.instances = [];
  localStorageStore.clear();
});
