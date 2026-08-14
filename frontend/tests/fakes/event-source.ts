export interface FakeEvent {
  lastEventId?: string
  type?: string
  data?: string
}

class FakeEventSource extends EventTarget {
  static instances: FakeEventSource[] = []
  readonly url: string
  readyState = 0
  onopen: ((event: Event) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null

  constructor(url: string) {
    super()
    this.url = url
    FakeEventSource.instances.push(this)
  }

  close() { this.readyState = 2 }

  open() {
    if (this.readyState === 2) return
    this.readyState = 1
    const event = new Event('open')
    this.onopen?.(event)
    this.dispatchEvent(event)
  }

  emit({ lastEventId = '', type = 'message', data = '{}' }: FakeEvent) {
    if (this.readyState === 2) return
    const event = new MessageEvent(type, { data, lastEventId })
    if (type === 'message') this.onmessage?.(event)
    this.dispatchEvent(event)
  }

  fail() {
    if (this.readyState === 2) return
    const event = new Event('error')
    this.onerror?.(event)
    this.dispatchEvent(event)
  }
}

export function installFakeEventSource() {
  FakeEventSource.instances = []
  const original = globalThis.EventSource
  Object.defineProperty(globalThis, 'EventSource', { configurable: true, value: FakeEventSource })
  return {
    emit(event: FakeEvent) { FakeEventSource.instances.at(-1)?.emit(event) },
    open() { FakeEventSource.instances.at(-1)?.open() },
    fail() { FakeEventSource.instances.at(-1)?.fail() },
    latestUrl: () => FakeEventSource.instances.at(-1)?.url ?? '',
    instances: () => FakeEventSource.instances,
    restore() { Object.defineProperty(globalThis, 'EventSource', { configurable: true, value: original }) },
  }
}
