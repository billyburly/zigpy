import asyncio
from unittest import mock

from asynctest import CoroutineMock
import pytest
from zigpy import device, endpoint
from zigpy.application import ControllerApplication
import zigpy.exceptions
import zigpy.types as t
from zigpy.zdo import types as zdo_t


@pytest.fixture
def dev(monkeypatch):
    monkeypatch.setattr(device, "APS_REPLY_TIMEOUT_EXTENDED", 0.1)
    app_mock = mock.MagicMock(spec_set=ControllerApplication)
    app_mock.remove.side_effect = asyncio.coroutine(mock.MagicMock())
    ieee = t.EUI64(map(t.uint8_t, [0, 1, 2, 3, 4, 5, 6, 7]))
    return device.Device(app_mock, ieee, 65535)


@pytest.mark.asyncio
async def test_initialize(monkeypatch, dev):
    async def mockrequest(nwk, tries=None, delay=None):
        return [0, None, [1, 2]]

    async def mockepinit(self):
        self.status = endpoint.Status.ZDO_INIT
        return

    monkeypatch.setattr(endpoint.Endpoint, "initialize", mockepinit)
    gnd = asyncio.coroutine(mock.MagicMock())
    dev.get_node_descriptor = mock.MagicMock(side_effect=gnd)

    dev.zdo.Active_EP_req = mockrequest
    await dev._initialize()

    assert dev.status > device.Status.NEW
    assert 1 in dev.endpoints
    assert 2 in dev.endpoints
    assert dev._application.device_initialized.call_count == 1


@pytest.mark.asyncio
async def test_initialize_fail(dev):
    async def mockrequest(nwk, tries=None, delay=None):
        return [1]

    dev.zdo.Active_EP_req = mockrequest
    gnd = asyncio.coroutine(mock.MagicMock())
    dev.get_node_descriptor = mock.MagicMock(side_effect=gnd)
    await dev._initialize()

    assert dev.status == device.Status.NEW


@pytest.mark.asyncio
async def test_initialize_ep_failed(monkeypatch, dev):
    async def mockrequest(req, nwk, tries=None, delay=None):
        return [0, None, [1, 2]]

    async def mockepinit(self):
        raise AttributeError

    monkeypatch.setattr(endpoint.Endpoint, "initialize", mockepinit)

    dev.zdo.request = mockrequest
    await dev._initialize()

    assert dev.status == device.Status.ZDO_INIT
    assert dev.application.listener_event.call_count == 1
    assert dev.application.listener_event.call_args[0][0] == "device_init_failure"
    assert dev.application.remove.call_count == 1


@pytest.mark.asyncio
async def test_request(dev):
    seq = mock.sentinel.tsn

    async def mock_req(*args, **kwargs):
        dev._pending[seq].result.set_result(mock.sentinel.result)
        return 0, ""

    dev.application.request.side_effect = mock_req
    assert dev.last_seen is None
    r = await dev.request(1, 2, 3, 3, seq, b"")
    assert r is mock.sentinel.result
    assert dev._application.request.call_count == 1
    assert dev.last_seen is not None


@pytest.mark.asyncio
async def test_failed_request(dev):
    assert dev.last_seen is None
    dev._application.request = CoroutineMock(return_value=(1, "error"))
    with pytest.raises(zigpy.exceptions.DeliveryError):
        await dev.request(1, 2, 3, 4, 5, b"")
    assert dev.last_seen is None


def test_radio_details(dev):
    dev.radio_details(1, 2)
    assert dev.lqi == 1
    assert dev.rssi == 2


def test_deserialize(dev):
    ep = dev.add_endpoint(3)
    ep.deserialize = mock.MagicMock()
    dev.deserialize(3, 1, b"")
    assert ep.deserialize.call_count == 1


def test_handle_message_no_endpoint(dev):
    dev.handle_message(99, 98, 97, 97, b"aabbcc")


def test_handle_message(dev):
    ep = dev.add_endpoint(3)
    hdr = mock.MagicMock()
    hdr.tsn = mock.sentinel.tsn
    hdr.is_reply = mock.sentinel.is_reply
    dev.deserialize = mock.MagicMock(return_value=[hdr, mock.sentinel.args])
    ep.handle_message = mock.MagicMock()
    dev.handle_message(99, 98, 3, 3, b"abcd")
    assert ep.handle_message.call_count == 1


def test_handle_message_reply(dev):
    ep = dev.add_endpoint(3)
    ep.handle_message = mock.MagicMock()
    tsn = mock.sentinel.tsn
    req_mock = mock.MagicMock()
    dev._pending[tsn] = req_mock
    hdr_1 = mock.MagicMock()
    hdr_1.tsn = tsn
    hdr_1.command_id = mock.sentinel.command_id
    hdr_1.is_reply = True
    hdr_2 = mock.MagicMock()
    hdr_2.tsn = mock.sentinel.another_tsn
    hdr_2.command_id = mock.sentinel.command_id
    hdr_2.is_reply = True
    dev.deserialize = mock.MagicMock(
        side_effect=(
            (hdr_1, mock.sentinel.args),
            (hdr_2, mock.sentinel.args),
            (hdr_1, mock.sentinel.args),
        )
    )
    dev.handle_message(99, 98, 3, 3, b"abcd")
    assert ep.handle_message.call_count == 0
    assert req_mock.result.set_result.call_count == 1
    assert req_mock.result.set_result.call_args[0][0] is mock.sentinel.args

    req_mock.reset_mock()
    dev.handle_message(99, 98, 3, 3, b"abcd")
    assert ep.handle_message.call_count == 1
    assert ep.handle_message.call_args[0][-1] is mock.sentinel.args
    assert req_mock.result.set_result.call_count == 0

    req_mock.reset_mock()
    req_mock.result.set_result.side_effect = asyncio.InvalidStateError
    ep.handle_message.reset_mock()
    dev.handle_message(99, 98, 3, 3, b"abcd")
    assert ep.handle_message.call_count == 0
    assert req_mock.result.set_result.call_count == 1


def test_handle_message_deserialize_error(dev):
    ep = dev.add_endpoint(3)
    dev.deserialize = mock.MagicMock(side_effect=ValueError)
    ep.handle_message = mock.MagicMock()
    dev.handle_message(99, 98, 3, 3, b"abcd")
    assert ep.handle_message.call_count == 0


def test_endpoint_getitem(dev):
    ep = dev.add_endpoint(3)
    assert dev[3] is ep

    with pytest.raises(KeyError):
        dev[1]


@pytest.mark.asyncio
async def test_broadcast():
    from zigpy.profiles import zha

    app = mock.MagicMock()
    app.broadcast.side_effect = asyncio.coroutine(mock.MagicMock())
    app.ieee = t.EUI64(map(t.uint8_t, [8, 9, 10, 11, 12, 13, 14, 15]))

    (profile, cluster, src_ep, dst_ep, data) = (
        zha.PROFILE_ID,
        1,
        2,
        3,
        b"\x02\x01\x00",
    )
    await device.broadcast(app, profile, cluster, src_ep, dst_ep, 0, 0, 123, data)

    assert app.broadcast.call_count == 1
    assert app.broadcast.call_args[0][0] == profile
    assert app.broadcast.call_args[0][1] == cluster
    assert app.broadcast.call_args[0][2] == src_ep
    assert app.broadcast.call_args[0][3] == dst_ep
    assert app.broadcast.call_args[0][7] == data


async def _get_node_descriptor(dev, zdo_success=True, request_success=True):
    async def mockrequest(nwk, tries=None, delay=None):
        if not request_success:
            raise asyncio.TimeoutError

        status = 0 if zdo_success else 1
        return [status, nwk, zdo_t.NodeDescriptor.deserialize(b"abcdefghijklm")[0]]

    dev.zdo.Node_Desc_req = mock.MagicMock(side_effect=mockrequest)
    return await dev.get_node_descriptor()


@pytest.mark.asyncio
async def test_get_node_descriptor(dev):
    nd = await _get_node_descriptor(dev, zdo_success=True, request_success=True)

    assert nd is not None
    assert isinstance(nd, zdo_t.NodeDescriptor)
    assert dev.zdo.Node_Desc_req.call_count == 1


@pytest.mark.asyncio
async def test_get_node_descriptor_no_reply(dev):
    nd = await _get_node_descriptor(dev, zdo_success=True, request_success=False)

    assert nd is None
    assert dev.zdo.Node_Desc_req.call_count == 1


@pytest.mark.asyncio
async def test_get_node_descriptor_fail(dev):
    nd = await _get_node_descriptor(dev, zdo_success=False, request_success=True)

    assert nd is None
    assert dev.zdo.Node_Desc_req.call_count == 1


@pytest.mark.asyncio
async def test_add_to_group(dev, monkeypatch):
    grp_id, grp_name = 0x1234, "test group 0x1234"
    epmock = mock.MagicMock(spec_set=endpoint.Endpoint)
    monkeypatch.setattr(endpoint, "Endpoint", mock.MagicMock(return_value=epmock))
    epmock.add_to_group.side_effect = asyncio.coroutine(mock.MagicMock())

    dev.add_endpoint(3)
    dev.add_endpoint(4)

    await dev.add_to_group(grp_id, grp_name)
    assert epmock.add_to_group.call_count == 2
    assert epmock.add_to_group.call_args[0][0] == grp_id
    assert epmock.add_to_group.call_args[0][1] == grp_name


@pytest.mark.asyncio
async def test_remove_from_group(dev, monkeypatch):
    grp_id = 0x1234
    epmock = mock.MagicMock(spec_set=endpoint.Endpoint)
    monkeypatch.setattr(endpoint, "Endpoint", mock.MagicMock(return_value=epmock))
    epmock.remove_from_group.side_effect = asyncio.coroutine(mock.MagicMock())

    dev.add_endpoint(3)
    dev.add_endpoint(4)

    await dev.remove_from_group(grp_id)
    assert epmock.remove_from_group.call_count == 2
    assert epmock.remove_from_group.call_args[0][0] == grp_id
