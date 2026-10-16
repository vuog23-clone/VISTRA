"""Observe existing SDK calls without changing application prompts or clients."""

from time import perf_counter


class Meter:
    def __init__(self):
        self.events = []

    def wrap(self, client):
        meter = self
        class Responses:
            def __getattr__(self, name):
                method = getattr(client.responses, name)
                if name not in {'create', 'parse'}:
                    return method
                def call(**options):
                    event = {'method': name, 'requested_model': options.get('model'), 'usage': None}
                    start = perf_counter()
                    try:
                        response = method(**options)
                        event.update(status=getattr(response, 'status', None), served_model=getattr(response, 'model', None))
                        usage = getattr(response, 'usage', None)
                        if usage is not None:
                            event['usage'] = {'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens,
                                'cached_input_tokens': getattr(getattr(usage, 'input_tokens_details', None), 'cached_tokens', None)}
                        return response
                    except Exception as error:
                        event.update(status='error', error_type=type(error).__name__)
                        raise
                    finally:
                        event['elapsed_seconds'] = perf_counter()-start
                        meter.events.append(event)
                return call
        class Client:
            responses = Responses()
            def __getattr__(self, name):
                return getattr(client, name)
        return Client()
