

import immudb.datatypesv2 as datatypesv2


def convertRequest(fromDataClass: datatypesv2.GRPCTransformable):
    return fromDataClass.getGRPC()
    

def convertResponse(fromResponse):
    if fromResponse.__class__.__name__ == "RepeatedCompositeContainer":
        all = []
        for item in fromResponse:
            all.append(convertResponse(item))
        return all
    schemaFrom = datatypesv2.__dict__.get(fromResponse.__class__.__name__, None)
    if schemaFrom:
        construct = dict()
        for field in fromResponse.ListFields():
            construct[field[0].name] = convertResponse(field[1])
        return schemaFrom(**construct)
    else:
        return fromResponse

