import asyncio
from channels_redis.core import RedisChannelLayer

async def main():
    layer = RedisChannelLayer(hosts=[{'address': 'redis://127.0.0.1:6379'}])
    print('Testing RedisChannelLayer...')
    channel = await layer.new_channel('test')
    print('Channel:', channel)
    await layer.send(channel, {'type': 'test.message', 'message': 'Hello from Django Channels'})
    print('Message sent successfully.')
    message = await layer.receive(channel)
    print('Message received:', message)
    await layer.close_pools()
    print('RedisChannelLayer test PASSED.')

asyncio.run(main())
