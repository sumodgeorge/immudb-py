# Copyright 2021 CodeNotary, Inc. All rights reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#       http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from io import BytesIO
from typing import Generator, List, Tuple, Union
import grpc
from google.protobuf import empty_pb2 as google_dot_protobuf_dot_empty__pb2

from immudb import grpcutils
from immudb.grpc.schema_pb2 import Chunk, EntriesSpec, EntryTypeSpec, TxScanRequest
from immudb.handler import (batchGet, batchSet, changePassword, changePermission, createUser,
                            currentRoot, createDatabase, databaseList, deleteKeys, useDatabase,
                            get, listUsers, sqldescribe, verifiedGet, verifiedSet, setValue, history,
                            scan, reference, verifiedreference, zadd, verifiedzadd,
                            zscan, healthcheck, health, txbyid, verifiedtxbyid, sqlexec, sqlquery,
                            listtables, execAll, transaction)
from immudb.rootService import *
from immudb.grpc import schema_pb2_grpc
import warnings
import ecdsa
from immudb.datatypes import DeleteKeysRequest
from immudb.embedded.store import KVMetadata
import threading
import queue
import immudb.datatypesv2 as datatypesv2
import immudb.dataconverter as dataconverter

import datetime

from immudb.streamsutils import FullKeyValue, KeyHeader, StreamReader, ValueChunk, ValueChunkHeader, BufferedStreamReader


class ImmudbClient:

    def __init__(self, immudUrl=None, rs: RootService = None, publicKeyFile: str = None, timeout=None, max_grpc_message_length = None):
        """Immudb client

        Args:
            immudbUrl (str, optional): url in format host:port, ex. localhost:3322 pointing to your immudb instance. Defaults to None.
            rs (RootService, optional): object that implements RootService - to be allow to verify requests. Defaults to None.
            publicKeyFile (str, optional): path to the public key that would be used to authenticate requests with. Defaults to None.
            timeout (int, optional): global timeout for GRPC requests, if None - it would hang until server respond. Defaults to None.
            max_grpc_message_length (int, optional): max size for message from the server. If None - it would set defaults (4mb).
        """
        if immudUrl is None:
            immudUrl = "localhost:3322"
        self.timeout = timeout
        options = []
        if max_grpc_message_length:
            options = [('grpc.max_receive_message_length', max_grpc_message_length)]
            self.channel = grpc.insecure_channel(immudUrl, options = options)
        else:
            self.channel = grpc.insecure_channel(immudUrl)
        self._resetStub()
        if rs is None:
            self._rs = RootService()
        else:
            self._rs = rs
        self._url = immudUrl
        self._vk = None
        if publicKeyFile:
            self.loadKey(publicKeyFile)

    def loadKey(self, kfile: str):
        """Loads public key from path

        Args:
            kfile (str): key file path
        """
        with open(kfile) as f:
            self._vk = ecdsa.VerifyingKey.from_pem(f.read())
    def loadKeyFromString(self, key: str):
        """Loads public key from parameter

        Args:
            key (str): key 
        """
        self._vk = ecdsa.VerifyingKey.from_pem(key)

    def shutdown(self):
        """Shutdowns client
        """
        self.channel.close()
        self.channel = None
        self.intercept_channel.close
        self.intercept_channel = None
        self._rs = None

    def set_session_id_interceptor(self, openSessionResponse):
        sessionId = openSessionResponse.sessionID
        self.headersInterceptors = [
            grpcutils.header_adder_interceptor('sessionid', sessionId)]
        return self.get_intercepted_stub()

    def set_token_header_interceptor(self, response):
        try:
            token = response.token
        except AttributeError:
            token = response.reply.token
        self.headersInterceptors = [
            grpcutils.header_adder_interceptor(
                'authorization', "Bearer " + token
            )
        ]
        return self.get_intercepted_stub()

    def get_intercepted_stub(self):
        allInterceptors = self.headersInterceptors + self.clientInterceptors
        intercepted, newStub = grpcutils.get_intercepted_stub(
            self.channel, allInterceptors)
        self.intercept_channel = intercepted
        return newStub

    @property
    def stub(self):
        return self._stub

# from here on same order as in Golang ImmuClient interface (pkg/client/client.go)

    # Not implemented: disconnect
    # Not implemented: isConnected
    # Not implemented: waitForHealthCheck
    def healthCheck(self):
        """Retrieves health response of immudb

        Returns:
            HealthResponse: contains status and version
        """
        return healthcheck.call(self._stub, self._rs)

    # Not implemented: connect
    def _convertToBytes(self, what):
        if (type(what) != bytes):
            return bytes(what, encoding='utf-8')
        return what

    def login(self, username, password, database=b"defaultdb"):
        """Logins into immudb

        Args:
            username (str): username
            password (str): password for user
            database (bytes, optional): database to switch to. Defaults to b"defaultdb".

        Raises:
            Exception: if user tries to login on shut down client

        Returns:
            LoginResponse: contains token and warning if any
        """
        convertedUsername = self._convertToBytes(username)
        convertedPassword = self._convertToBytes(password)
        convertedDatabase = self._convertToBytes(database)
        req = schema_pb2_grpc.schema__pb2.LoginRequest(
            user=convertedUsername, password=convertedPassword)
        login_response = None
        try:
            login_response = schema_pb2_grpc.schema__pb2.LoginResponse = \
                self._stub.Login(
                    req
                )
        except ValueError as e:
            raise Exception(
                "Attempted to login on termninated client, channel has been shutdown") from e

        self._stub = self.set_token_header_interceptor(login_response)
        # Select database, modifying stub function accordingly
        request = schema_pb2_grpc.schema__pb2.Database(
            databaseName=convertedDatabase)
        resp = self._stub.UseDatabase(request)
        self._stub = self.set_token_header_interceptor(resp)

        self._rs.init("{}/{}".format(self._url, database), self._stub)
        return login_response

    def logout(self):
        """Logouts all sessions
        """
        self._stub.Logout(google_dot_protobuf_dot_empty__pb2.Empty())
        self._resetStub()

    def _resetStub(self):
        self.headersInterceptors = []
        self.clientInterceptors = []
        if (self.timeout != None):
            self.clientInterceptors.append(
                grpcutils.timeout_adder_interceptor(self.timeout))
        self._stub = schema_pb2_grpc.ImmuServiceStub(self.channel)
        self._stub = self.get_intercepted_stub()

    def keepAlive(self):
        """Sends keep alive packet
        """
        self._stub.KeepAlive(google_dot_protobuf_dot_empty__pb2.Empty())

    def openManagedSession(self, username, password, database=b"defaultdb", keepAliveInterval=60):
        """Opens managed session and returns ManagedSession object within you can manage SQL transactions


        example of usage:
        with client.openManagedSession(username, password) as session:
            session.newTx()

        Check handler/transaction.py

        Args:
            username (str): username
            password (str): password for user
            database (bytes, optional): name of database. Defaults to b"defaultdb".
            keepAliveInterval (int, optional): specifies how often keep alive packet should be sent. Defaults to 60.

        Returns:
            ManagedSession: managed Session object
        """
        class ManagedSession:
            def __init__(this, keepAliveInterval):
                this.keepAliveInterval = keepAliveInterval
                this.keepAliveStarted = False
                this.keepAliveProcess = None
                this.queue = queue.Queue()

            def manage(this):
                while this.keepAliveStarted:
                    try:
                        what = this.queue.get(True, this.keepAliveInterval)
                    except queue.Empty:
                        self.keepAlive()

            def __enter__(this):
                interface = self.openSession(username, password, database)
                this.keepAliveStarted = True
                this.keepAliveProcess = threading.Thread(target=this.manage)
                this.keepAliveProcess.start()
                return interface

            def __exit__(this, type, value, traceback):
                this.keepAliveStarted = False
                this.queue.put(b'0')
                self.closeSession()

        return ManagedSession(keepAliveInterval)

    def openSession(self, username, password, database=b"defaultdb"):
        """Opens unmanaged session. Unmanaged means that you have to send keep alive packets yourself.
        Managed session does it for you

        Args:
            username (str): username
            password (str): password
            database (bytes, optional): database name to switch to. Defaults to b"defaultdb".

        Returns:
            Tx: Tx object (handlers/transaction.py)
        """
        convertedUsername = self._convertToBytes(username)
        convertedPassword = self._convertToBytes(password)
        convertedDatabase = self._convertToBytes(database)
        req = schema_pb2_grpc.schema__pb2.OpenSessionRequest(
            username=convertedUsername,
            password=convertedPassword,
            databaseName=convertedDatabase
        )
        session_response = self._stub.OpenSession(
            req)
        self._stub = self.set_session_id_interceptor(session_response)
        return transaction.Tx(self._stub, session_response, self.channel)

    def closeSession(self):
        """Closes unmanaged session
        """
        self._stub.CloseSession(google_dot_protobuf_dot_empty__pb2.Empty())
        self._resetStub()

    def createUser(self, user, password, permission, database):
        """Creates user specified in parameters

        Args:
            user (str): username
            password (str): password
            permission (int): permissions (constants.PERMISSION_X)
            database (str): database name

        """
        request = schema_pb2_grpc.schema__pb2.CreateUserRequest(
            user=bytes(user, encoding='utf-8'),
            password=bytes(password, encoding='utf-8'),
            permission=permission,
            database=database
        )
        return createUser.call(self._stub, self._rs, request)

    def listUsers(self):
        """Returns all users on database

        Returns:
            ListUserResponse: List containing all users
        """
        return listUsers.call(self._stub)

    def changePassword(self, user, newPassword, oldPassword):
        """Changes password for user

        Args:
            user (str): username
            newPassword (str): new password
            oldPassword (str): old password

        """
        request = schema_pb2_grpc.schema__pb2.ChangePasswordRequest(
            user=bytes(user, encoding='utf-8'),
            newPassword=bytes(newPassword, encoding='utf-8'),
            oldPassword=bytes(oldPassword, encoding='utf-8')
        )
        return changePassword.call(self._stub, self._rs, request)

    def changePermission(self, action, user, database, permission):
        """Changes permission for user

        Args:
            action (int): GRANT or REVOKE - see constants/PERMISSION_GRANT
            user (str): username
            database (str): database name
            permission (int): permission to revoke/ grant - see constants/PERMISSION_GRANT

        Returns:
            _type_: _description_
        """
        return changePermission.call(self._stub, self._rs, action, user, database, permission)

    def databaseList(self):
        """Returns database list

        Returns:
            list[str]: database names
        """
        dbs = databaseList.call(self._stub, self._rs, None)
        return [x.databaseName for x in dbs.dblist.databases]

    def createDatabase(self, dbName: bytes):
        """Creates database

        Args:
            dbName (bytes): name of database

        """
        request = schema_pb2_grpc.schema__pb2.Database(databaseName=dbName)
        return createDatabase.call(self._stub, self._rs, request)

    def createDatabaseV2(self, name: str, settings: datatypesv2.DatabaseNullableSettings, ifNotExists: bool) -> datatypesv2.CreateDatabaseResponse:
        request = datatypesv2.CreateDatabaseRequest(name = name, settings = settings, ifNotExists = ifNotExists)
        resp = self._stub.CreateDatabaseV2(request._getGRPC())
        return dataconverter.convertResponse(resp)

    def databaseListV2(self) -> datatypesv2.DatabaseListResponseV2:
        req = datatypesv2.DatabaseListRequestV2()
        resp = self._stub.DatabaseListV2(req._getGRPC())
        return dataconverter.convertResponse(resp)

    def loadDatabase(self, database: str) -> datatypesv2.LoadDatabaseResponse:
        req = datatypesv2.LoadDatabaseRequest(database)
        resp = self._stub.LoadDatabase(req._getGRPC())
        return dataconverter.convertResponse(resp)

    def unloadDatabase(self, database: str) -> datatypesv2.UnloadDatabaseResponse:
        req = datatypesv2.UnloadDatabaseRequest(database)
        resp = self._stub.UnloadDatabase(req._getGRPC())
        return dataconverter.convertResponse(resp)

    def deleteDatabase(self, database: str) -> datatypesv2.DeleteDatabaseResponse:
        req = datatypesv2.DeleteDatabaseResponse(database)
        resp = self._stub.DeleteDatabase(req._getGRPC())
        return dataconverter.convertResponse(resp)

    def updateDatabaseV2(self, database: str, settings: datatypesv2.DatabaseNullableSettings) -> datatypesv2.UpdateDatabaseResponse:
        request = datatypesv2.UpdateDatabaseRequest(database, settings)
        resp = self._stub.UpdateDatabaseV2(request._getGRPC())
        return dataconverter.convertResponse(resp)

    def useDatabase(self, dbName: bytes):
        """Switches database

        Args:
            dbName (bytes): database name

        """
        request = schema_pb2_grpc.schema__pb2.Database(databaseName=dbName)
        resp = useDatabase.call(self._stub, self._rs, request)
        # modify header token accordingly
        self._stub = self.set_token_header_interceptor(resp)
        self._rs.init(dbName, self._stub)
        return resp


    def getDatabaseSettingsV2(self) -> datatypesv2.DatabaseSettingsResponse:
        req = datatypesv2.DatabaseSettingsRequest()
        resp = self._stub.GetDatabaseSettingsV2(req._getGRPC())
        return dataconverter.convertResponse(resp)

    def setActiveUser(self, active: bool, username: str) -> bool:
        req = datatypesv2.SetActiveUserRequest(active, username)
        resp = self._stub.SetActiveUser(req._getGRPC())
        return resp == google_dot_protobuf_dot_empty__pb2.Empty()

    def flushIndex(self, cleanupPercentage: float, synced: bool) -> datatypesv2.FlushIndexResponse:
        req = datatypesv2.FlushIndexRequest(cleanupPercentage, synced)
        resp = self._stub.FlushIndex(req._getGRPC())
        return dataconverter.convertResponse(resp)

    def compactIndex(self):
        """Starts index compaction
        """
        resp = self._stub.CompactIndex(google_dot_protobuf_dot_empty__pb2.Empty())
        return resp == google_dot_protobuf_dot_empty__pb2.Empty()

    def health(self):
        """Retrieves health response of immudb

        Returns:
            HealthResponse: contains status and version
        """
        return health.call(self._stub, self._rs)

    def currentState(self):
        """Return current state of immudb (proof)

        Returns:
            State: state of immudb
        """
        return currentRoot.call(self._stub, self._rs, None)

    def set(self, key: bytes, value: bytes):
        """Sets key into value in database

        Args:
            key (bytes): key
            value (bytes): value

        Returns:
            SetResponse: response of request
        """
        return setValue.call(self._stub, self._rs, key, value)

    def verifiedSet(self, key: bytes, value: bytes):
        """Sets key into value in database, and additionally checks it with state saved before

        Args:
            key (bytes): key
            value (bytes): value

        Returns:
            SetResponse: response of request
        """
        return verifiedSet.call(self._stub, self._rs, key, value, self._vk)

    def expireableSet(self, key: bytes, value: bytes, expiresAt: datetime.datetime):
        """Sets key into value in database with additional expiration

        Args:
            key (bytes): key
            value (bytes): value
            expiresAt (datetime.datetime): Expiration time

        Returns:
            SetResponse: response of request
        """
        metadata = KVMetadata()
        metadata.ExpiresAt(expiresAt)
        return setValue.call(self._stub, self._rs, key, value, metadata)

    def get(self, key: bytes, atRevision: int = None):
        """Gets value for key

        Args:
            key (bytes): key
            atRevision (int, optional): gets value at revision specified by this argument. It could be relative (-1, -2), or fixed (32). Defaults to None.

        Returns:
            GetResponse: contains tx, value, key and revision
        """
        return get.call(self._stub, self._rs, key, atRevision=atRevision)

    # Not implemented: getSince
    # Not implemented: getAt

    def verifiedGet(self, key: bytes, atRevision: int = None):
        return verifiedGet.call(self._stub, self._rs, key, verifying_key=self._vk, atRevision=atRevision)

    def verifiedGetSince(self, key: bytes, sinceTx: int):
        return verifiedGet.call(self._stub, self._rs, key, sinceTx=sinceTx, verifying_key=self._vk)

    def verifiedGetAt(self, key: bytes, atTx: int):
        return verifiedGet.call(self._stub, self._rs, key, atTx, self._vk)

    def history(self, key: bytes, offset: int, limit: int, sortorder: bool):
        return history.call(self._stub, self._rs, key, offset, limit, sortorder)

    def zAdd(self, zset: bytes, score: float, key: bytes, atTx: int = 0):
        return zadd.call(self._stub, self._rs, zset, score, key, atTx)

    def verifiedZAdd(self, zset: bytes, score: float, key: bytes, atTx: int = 0):
        return verifiedzadd.call(self._stub, self._rs, zset, score, key, atTx, self._vk)

    def scan(self, key: bytes, prefix: bytes, desc: bool, limit: int, sinceTx: int = None):
        return scan.call(self._stub, self._rs, key, prefix, desc, limit, sinceTx)

    def zScan(self, zset: bytes, seekKey: bytes, seekScore: float,
              seekAtTx: int, inclusive: bool, limit: int, desc: bool, minscore: float,
              maxscore: float, sinceTx=None, nowait=False):
        return zscan.call(self._stub, self._rs, zset, seekKey, seekScore,
                          seekAtTx, inclusive, limit, desc, minscore,
                          maxscore, sinceTx, nowait)

    def txById(self, tx: int):
        return txbyid.call(self._stub, self._rs, tx)

    def verifiedTxById(self, tx: int):
        return verifiedtxbyid.call(self._stub, self._rs, tx, self._vk)

    # Not implemented: txByIDWithSpec

    def txScan(self, initialTx: int, limit: int = 999, desc: bool = False, entriesSpec: datatypesv2.EntriesSpec = None, sinceTx: int = 0, noWait: bool = False) -> datatypesv2.TxList:
        req = datatypesv2.TxScanRequest(initialTx, limit, desc, entriesSpec, sinceTx, noWait)
        resp = self._stub.TxScan(req._getGRPC())
        return dataconverter.convertResponse(resp)

    def serverInfo(self) -> datatypesv2.ServerInfoResponse:
        req = datatypesv2.ServerInfoRequest()
        resp = self._stub.ServerInfo(req._getGRPC())
        return dataconverter.convertResponse(resp)

    def databaseHealth(self) -> datatypesv2.DatabaseHealthResponse:
        req = google_dot_protobuf_dot_empty__pb2.Empty()
        resp = self._stub.DatabaseHealth(req)
        return dataconverter.convertResponse(resp)

    def setAll(self, kv: dict):
        return batchSet.call(self._stub, self._rs, kv)

    def getAll(self, keys: list):
        resp = batchGet.call(self._stub, self._rs, keys)
        return {key: value.value for key, value in resp.items()}

    def delete(self, req: DeleteKeysRequest):
        return deleteKeys.call(self._stub, req)

    def execAll(self, ops: list, noWait=False):
        return execAll.call(self._stub, self._rs, ops, noWait)

    def setReference(self, referredkey: bytes, newkey:  bytes):
        return reference.call(self._stub, self._rs, referredkey, newkey)

    def verifiedSetReference(self, referredkey: bytes, newkey:  bytes):
        return verifiedreference.call(self._stub, self._rs, referredkey, newkey, verifying_key=self._vk)

    # Not implemented: setReferenceAt
    # Not implemented: verifiedSetReferenceAt

    # Not implemented: dump


    def _rawStreamGet(self, key: bytes, atTx: int = None, sinceTx: int = None, noWait: bool = None, atRevision: int = None) -> Generator[Union[KeyHeader, ValueChunk], None, None]:
        req = datatypesv2.KeyRequest(key = key, atTx = atTx, sinceTx = sinceTx, noWait = noWait, atRevision = atRevision)
        resp = self._stub.streamGet(req._getGRPC())
        reader = StreamReader(resp)
        for it in reader.chunks():
            yield it

    def streamGet(self, key: bytes, atTx: int = None, sinceTx: int = None, noWait: bool = None, atRevision: int = None) -> Tuple[bytes, BufferedStreamReader]:
        req = datatypesv2.KeyRequest(key = key, atTx = atTx, sinceTx = sinceTx, noWait = noWait, atRevision = atRevision)
        resp = self._stub.streamGet(req._getGRPC())
        reader = StreamReader(resp)
        chunks = reader.chunks()
        keyHeader = next(chunks)
        valueHeader = next(chunks)
        return keyHeader.key, BufferedStreamReader(chunks, valueHeader, resp)

    def streamGetFull(self, key: bytes, atTx: int = None, sinceTx: int = None, noWait: bool = None, atRevision: int = None) -> datatypesv2.KeyValue:
        req = datatypesv2.KeyRequest(key = key, atTx = atTx, sinceTx = sinceTx, noWait = noWait, atRevision = atRevision)
        resp = self._stub.streamGet(req._getGRPC())
        reader = StreamReader(resp)
        key = None
        value = b''
        chunks = reader.chunks()
        key = next(chunks).key
        for it in chunks:
            value += it.chunk
        return datatypesv2.KeyValue(key, value)

    # def streamVerifiableGet(self, key: bytes, atTx: int = None, sinceTx: int = None, noWait: bool = None, atRevision: int = None, proveSinceTx: int = None):
    #     req = datatypesv2.VerifiableGetRequest(keyRequest = datatypesv2.KeyRequest(
    #         key = key,
    #         atTx=atTx,
    #         sinceTx=sinceTx,
    #         noWait = noWait,
    #         atRevision=atRevision
    #     ), proveSinceTx=proveSinceTx)
    #     resp = self._stub.streamVerifiableGet(req._getGRPC())
    #     # reader = StreamReader(resp)
    #     for it in resp:
    #         yield it

    def _make_set_stream(self, buffer, key: bytes, length: int, chunkSize: int = 65536):
        yield Chunk(content = KeyHeader(key = key, length=len(key)).getInBytes())
        firstChunk = buffer.read(chunkSize)
        firstChunk = ValueChunkHeader(chunk = firstChunk, length = length).getInBytes()
        yield Chunk(content = firstChunk)
        chunk = buffer.read(chunkSize)
        while chunk:
            yield Chunk(content = chunk)
            chunk = buffer.read(chunkSize)

    def streamScan(self, seekKey: bytes = None, endKey: bytes = None, prefix: bytes = None, desc: bool = None, limit: int = None, sinceTx: int = None, noWait: bool = None, inclusiveSeek: bool = None, inclusiveEnd: bool = None, offset: int = None) -> Generator[datatypesv2.KeyValue, None, None]:
        req = datatypesv2.ScanRequest(seekKey=seekKey, endKey=endKey, prefix = prefix, desc = desc, limit = limit, sinceTx= sinceTx, noWait=noWait, inclusiveSeek=None, inclusiveEnd=None, offset=None)
        resp = self._stub.streamScan(req._getGRPC())
        key = None
        value = None
        for chunk in StreamReader(resp).chunks():
            if isinstance(chunk, KeyHeader):
                if key != None:
                    yield datatypesv2.KeyValue(key = key, value = value, metadata = None)
                key = chunk.key
                value = b''
            else:
                value += chunk.chunk

        if key != None and value != None: # situation when generator consumes all at first run, so it didn't yield first value
            yield datatypesv2.KeyValue(key = key, value = value, metadata = None)

    def streamScanBuffered(self, seekKey: bytes = None, endKey: bytes = None, prefix: bytes = None, desc: bool = None, limit: int = None, sinceTx: int = None, noWait: bool = None, inclusiveSeek: bool = None, inclusiveEnd: bool = None, offset: int = None) -> Generator[Tuple[bytes, BufferedStreamReader], None, None]:
        req = datatypesv2.ScanRequest(seekKey=seekKey, endKey=endKey, prefix = prefix, desc = desc, limit = limit, sinceTx= sinceTx, noWait=noWait, inclusiveSeek=inclusiveSeek, inclusiveEnd=inclusiveEnd, offset=offset)
        resp = self._stub.streamScan(req._getGRPC())
        key = None
        valueHeader = None

        streamReader = StreamReader(resp)
        chunks = streamReader.chunks()
        chunk = next(chunks)
        while chunk != None:
            if isinstance(chunk, KeyHeader):
                key = chunk.key
                valueHeader = next(chunks)
                yield key, BufferedStreamReader(chunks, valueHeader, resp)
            chunk = next(chunks, None)



    def _rawStreamSet(self, generator: Generator[Union[KeyHeader, ValueChunkHeader, ValueChunk], None, None]) -> datatypesv2.TxHeader:
        resp = self._stub.streamSet(generator)
        return dataconverter.convertResponse(resp)

    def streamSet(self, key: bytes, buffer, bufferLength: int, chunkSize: int = 65536) -> datatypesv2.TxHeader:
        resp = self._rawStreamSet(self._make_set_stream(buffer, key, bufferLength, chunkSize))
        return resp

    def streamSetFullValue(self, key: bytes, value: bytes, chunkSize: int = 65536) -> datatypesv2.TxHeader:
        resp = self._rawStreamSet(self._make_set_stream(BytesIO(value), key, len(value), chunkSize))
        return resp


    # Not implemented: exportTx
    # Not implemented: replicateTx

    def sqlExec(self, stmt, params={}, noWait=False):
        """Executes an SQL statement
        Args:
            stmt: a statement in immudb SQL dialect.
            params: a dictionary of parameters to replace in the statement
            noWait: whether to wait for indexing. Set to True for fast inserts.

        Returns:
            An object with two lists: ctxs and dtxs, including transaction
            metadata for both the catalog and the data store.

            Each element of both lists contains an object with the Transaction ID
            (id), timestamp (ts), and number of entries (nentries).
        """

        return sqlexec.call(self._stub, self._rs, stmt, params, noWait)

    def sqlQuery(self, query, params={}, columnNameMode=constants.COLUMN_NAME_MODE_NONE):
        """Queries the database using SQL
        Args:
            query: a query in immudb SQL dialect.
            params: a dictionary of parameters to replace in the query

        Returns:
            A list of table names. For example:

            ['table1', 'table2']
        """
        return sqlquery.call(self._stub, self._rs, query, params, columnNameMode)

    def listTables(self):
        """List all tables in the current database

        Returns:
            A list of table names. For example:

            ['table1', 'table2']
        """
        return listtables.call(self._stub, self._rs)

    def describeTable(self, table):
        return sqldescribe.call(self._stub, self._rs, table)

    # Not implemented: verifyRow

# deprecated
    def databaseCreate(self, dbName: bytes):
        warnings.warn("Call to deprecated databaseCreate. Use createDatabase instead",
                      category=DeprecationWarning,
                      stacklevel=2
                      )
        return self.createDatabase(dbName)

    def safeGet(self, key: bytes):  # deprecated
        warnings.warn("Call to deprecated safeGet. Use verifiedGet instead",
                      category=DeprecationWarning,
                      stacklevel=2
                      )
        return verifiedGet.call(self._stub, self._rs, key, verifying_key=self._vk)

    def databaseUse(self, dbName: bytes):  # deprecated
        warnings.warn("Call to deprecated databaseUse. Use useDatabase instead",
                      category=DeprecationWarning,
                      stacklevel=2
                      )
        return self.useDatabase(dbName)

    def safeSet(self, key: bytes, value: bytes):  # deprecated
        warnings.warn("Call to deprecated safeSet. Use verifiedSet instead",
                      category=DeprecationWarning,
                      stacklevel=2
                      )
        return verifiedSet.call(self._stub, self._rs, key, value)


# immudb-py only


    def getAllValues(self, keys: list):  # immudb-py only
        resp = batchGet.call(self._stub, self._rs, keys)
        return resp

    def getValue(self, key: bytes):  # immudb-py only
        ret = get.call(self._stub, self._rs, key)
        if ret is None:
            return None
        return ret.value
