import asyncio
from channels_redis.core import RedisChannelLayer

async def main():
    layer = RedisChannelLayer(hosts=[{'address': 'redis://127.0.0.1:6379'}])
    channel = await layer.new_channel('test')
    print('Channel:', channel)
    print('Adding channel to group...')
    await layer.group_add('bookings_dashboard', channel)
    print('GROUP ADD PASSED')
    print('Sending group message...')
    await layer.group_send('bookings_dashboard', {'type': 'booking.event', 'payload': {'message': 'Test'}})
    print('GROUP SEND PASSED')
    message = await layer.receive(channel)
    print('Received:', message)
    await layer.group_discard('bookings_dashboard', channel)
    print('GROUP DISCARD PASSED')
    await layer.close_pools()

asyncio.run(main())
