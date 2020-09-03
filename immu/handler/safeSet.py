import struct
from time import time
from dataclasses import dataclass

from immu.schema import schema_pb2
from immu.service import schema_pb2_grpc
from immu.rootService import RootService
from immu import constants, proofs, item

@dataclass
class SafeSetResponse:
    index: int
    leaf: bytes 
    root: bytes
    at: int
    inclusionPath: bytes
    consistencyPath: bytes
    verified: bool

def call(service: schema_pb2_grpc.ImmuServiceStub, rs: RootService, request: schema_pb2.SafeSetOptions):
    root = rs.get()
    index = schema_pb2.Index(index = root.index)

    content=schema_pb2.Content(
        timestamp=int(time()),
        payload=request.kv.value
        )
    
    skv=schema_pb2.StructuredKeyValue(key=request.kv.key, value=content)
    rawRequest=schema_pb2.SafeSetSVOptions( skv=skv, rootIndex=index)
    
    msg = service.SafeSetSV(rawRequest)
    digest=item.digest(msg.index, rawRequest.skv.key, rawRequest.skv.value.SerializeToString())
    if bytes(msg.leaf) != digest:
        raise Exception("Proof does not match the given item.")

    verified = proofs.verify(msg, bytes(msg.leaf), root)

    if verified:
        toCache = schema_pb2.Root(
            index = msg.index,
            root = msg.root
        )

        try:
            rs.set(toCache)
        except Exception as e:
            raise e

    return SafeSetResponse(
        index = msg.index,
        leaf = msg.leaf,
        root = msg.root,
        at = msg.at,
        inclusionPath = msg.inclusionPath,
        consistencyPath = msg.consistencyPath,
        verified = verified
    )
