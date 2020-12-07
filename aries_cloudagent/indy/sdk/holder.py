"""Indy SDK holder implementation."""

import json
import logging

from collections import OrderedDict
from typing import Sequence, Tuple, Union

import indy.anoncreds
from indy.error import ErrorCode, IndyError

from ...indy.sdk.wallet_setup import IndyOpenWallet
from ...ledger.base import BaseLedger
from ...storage.indy import IndySdkStorage
from ...storage.error import StorageError, StorageNotFoundError
from ...storage.record import StorageRecord
from ...wallet.error import WalletNotFoundError

from ..holder import IndyHolder, IndyHolderError

from .error import IndyErrorHandler
from .util import create_tails_reader

LOGGER = logging.getLogger(__name__)


class IndySdkHolder(IndyHolder):
    """Indy-SDK holder implementation."""

    def __init__(self, wallet: IndyOpenWallet):
        """
        Initialize an IndyHolder instance.

        Args:
            wallet: IndyOpenWallet instance

        """
        self.wallet = wallet

    async def create_credential_request(
        self, credential_offer: dict, credential_definition: dict, holder_did: str
    ) -> Tuple[str, str]:
        """
        Create a credential request for the given credential offer.

        Args:
            credential_offer: The credential offer to create request for
            credential_definition: The credential definition to create an offer for
            holder_did: the DID of the agent making the request

        Returns:
            A tuple of the credential request and credential request metadata

        """

        with IndyErrorHandler(
            "Error when creating credential request", IndyHolderError
        ):
            (
                credential_request_json,
                credential_request_metadata_json,
            ) = await indy.anoncreds.prover_create_credential_req(
                self.wallet.handle,
                holder_did,
                json.dumps(credential_offer),
                json.dumps(credential_definition),
                self.wallet.master_secret_id,
            )

        LOGGER.debug(
            "Created credential request. "
            "credential_request_json=%s credential_request_metadata_json=%s",
            credential_request_json,
            credential_request_metadata_json,
        )

        return credential_request_json, credential_request_metadata_json

    async def store_credential(
        self,
        credential_definition: dict,
        credential_data: dict,
        credential_request_metadata: dict,
        credential_attr_mime_types=None,
        credential_id: str = None,
        rev_reg_def: dict = None,
    ) -> str:
        """
        Store a credential in the wallet.

        Args:
            credential_definition: Credential definition for this credential
            credential_data: Credential data generated by the issuer
            credential_request_metadata: credential request metadata generated
                by the issuer
            credential_attr_mime_types: dict mapping attribute names to (optional)
                MIME types to store as non-secret record, if specified
            credential_id: optionally override the stored credential id
            rev_reg_def: revocation registry definition in json

        Returns:
            the ID of the stored credential

        """
        with IndyErrorHandler(
            "Error when storing credential in wallet", IndyHolderError
        ):
            credential_id = await indy.anoncreds.prover_store_credential(
                wallet_handle=self.wallet.handle,
                cred_id=credential_id,
                cred_req_metadata_json=json.dumps(credential_request_metadata),
                cred_json=json.dumps(credential_data),
                cred_def_json=json.dumps(credential_definition),
                rev_reg_def_json=json.dumps(rev_reg_def) if rev_reg_def else None,
            )

        if credential_attr_mime_types:
            mime_types = {
                attr: credential_attr_mime_types.get(attr)
                for attr in credential_data["values"]
                if attr in credential_attr_mime_types
            }
            if mime_types:
                record = StorageRecord(
                    type=IndyHolder.RECORD_TYPE_MIME_TYPES,
                    value=credential_id,
                    tags=mime_types,
                    id=f"{IndyHolder.RECORD_TYPE_MIME_TYPES}::{credential_id}",
                )
                indy_stor = IndySdkStorage(self.wallet)
                await indy_stor.add_record(record)

        return credential_id

    async def get_credentials(self, start: int, count: int, wql: dict):
        """
        Get credentials stored in the wallet.

        Args:
            start: Starting index
            count: Number of records to return
            wql: wql query dict

        """

        async def fetch(limit):
            """Fetch up to limit (default smaller of all remaining or 256) creds."""
            creds = []
            CHUNK = min(record_count, limit or record_count, IndyHolder.CHUNK)
            cardinality = min(limit or record_count, record_count)

            with IndyErrorHandler(
                "Error fetching credentials from wallet", IndyHolderError
            ):
                while len(creds) < cardinality:
                    batch = json.loads(
                        await indy.anoncreds.prover_fetch_credentials(
                            search_handle, CHUNK
                        )
                    )
                    creds.extend(batch)
                    if len(batch) < CHUNK:
                        break
            return creds

        with IndyErrorHandler(
            "Error when constructing wallet credential query", IndyHolderError
        ):
            (
                search_handle,
                record_count,
            ) = await indy.anoncreds.prover_search_credentials(
                self.wallet.handle, json.dumps(wql)
            )

            if start > 0:
                # must move database cursor manually
                await fetch(start)
            credentials = await fetch(count)

            await indy.anoncreds.prover_close_credentials_search(search_handle)

        return credentials

    async def get_credentials_for_presentation_request_by_referent(
        self,
        presentation_request: dict,
        referents: Sequence[str],
        start: int,
        count: int,
        extra_query: dict = {},
    ):
        """
        Get credentials stored in the wallet.

        Args:
            presentation_request: Valid presentation request from issuer
            referents: Presentation request referents to use to search for creds
            start: Starting index
            count: Maximum number of records to return
            extra_query: wql query dict

        """

        async def fetch(reft, limit):
            """Fetch up to limit (default smaller of all remaining or 256) creds."""
            creds = []
            CHUNK = min(IndyHolder.CHUNK, limit or IndyHolder.CHUNK)

            with IndyErrorHandler(
                "Error fetching credentials from wallet for presentation request",
                IndyHolderError,
            ):
                while not limit or len(creds) < limit:
                    batch = json.loads(
                        await indy.anoncreds.prover_fetch_credentials_for_proof_req(
                            search_handle, reft, CHUNK
                        )
                    )
                    creds.extend(batch)
                    if len(batch) < CHUNK:
                        break
            return creds

        with IndyErrorHandler(
            "Error when constructing wallet credential query", IndyHolderError
        ):
            search_handle = await (
                indy.anoncreds.prover_search_credentials_for_proof_req(
                    self.wallet.handle,
                    json.dumps(presentation_request),
                    json.dumps(extra_query),
                )
            )

            if not referents:
                referents = (
                    *presentation_request["requested_attributes"],
                    *presentation_request["requested_predicates"],
                )
            creds_dict = OrderedDict()

            try:
                for reft in referents:
                    # must move database cursor manually
                    if start > 0:
                        await fetch(reft, start)
                    credentials = await fetch(reft, count - len(creds_dict))

                    for cred in credentials:
                        cred_id = cred["cred_info"]["referent"]
                        if cred_id not in creds_dict:
                            cred["presentation_referents"] = {reft}
                            creds_dict[cred_id] = cred
                        else:
                            creds_dict[cred_id]["presentation_referents"].add(reft)
                    if len(creds_dict) >= count:
                        break
            finally:
                # Always close
                await indy.anoncreds.prover_close_credentials_search_for_proof_req(
                    search_handle
                )

        for cred in creds_dict.values():
            cred["presentation_referents"] = list(cred["presentation_referents"])

        creds_ordered = tuple(
            [
                cred
                for cred in sorted(
                    creds_dict.values(),
                    key=lambda c: (
                        c["cred_info"]["rev_reg_id"] or "",  # irrevocable 1st
                        c["cred_info"][
                            "referent"
                        ],  # should be descending by timestamp if we had it
                    ),
                )
            ]
        )[:count]
        return creds_ordered

    async def get_credential(self, credential_id: str) -> str:
        """
        Get a credential stored in the wallet.

        Args:
            credential_id: Credential id to retrieve

        """
        try:
            credential_json = await indy.anoncreds.prover_get_credential(
                self.wallet.handle, credential_id
            )
        except IndyError as err:
            if err.error_code == ErrorCode.WalletItemNotFound:
                raise WalletNotFoundError(
                    "Credential {} not found in wallet {}".format(
                        credential_id, self.wallet.name
                    )
                )
            else:
                raise IndyErrorHandler.wrap_error(
                    err,
                    f"Error when fetching credential {credential_id}",
                    IndyHolderError,
                ) from err

        return credential_json

    async def credential_revoked(
        self, ledger: BaseLedger, credential_id: str, fro: int = None, to: int = None
    ) -> bool:
        """
        Check ledger for revocation status of credential by cred id.

        Args:
            credential_id: Credential id to check

        """
        cred = json.loads(await self.get_credential(credential_id))
        rev_reg_id = cred["rev_reg_id"]

        if rev_reg_id:
            cred_rev_id = int(cred["cred_rev_id"])
            (rev_reg_delta, _) = await ledger.get_revoc_reg_delta(
                rev_reg_id,
                fro,
                to,
            )

            return cred_rev_id in rev_reg_delta["value"].get("revoked", [])
        else:
            return False

    async def delete_credential(self, credential_id: str):
        """
        Remove a credential stored in the wallet.

        Args:
            credential_id: Credential id to remove

        """
        try:
            indy_stor = IndySdkStorage(self.wallet)
            mime_types_record = await indy_stor.get_record(
                IndyHolder.RECORD_TYPE_MIME_TYPES,
                f"{IndyHolder.RECORD_TYPE_MIME_TYPES}::{credential_id}",
            )
            await indy_stor.delete_record(mime_types_record)
        except StorageNotFoundError:
            pass  # MIME types record not present: carry on

        try:
            await indy.anoncreds.prover_delete_credential(
                self.wallet.handle, credential_id
            )
        except IndyError as err:
            if err.error_code == ErrorCode.WalletItemNotFound:
                raise WalletNotFoundError(
                    "Credential {} not found in wallet {}".format(
                        credential_id, self.wallet.name
                    )
                )
            else:
                raise IndyErrorHandler.wrap_error(
                    err, "Error when deleting credential", IndyHolderError
                ) from err

    async def get_mime_type(
        self, credential_id: str, attr: str = None
    ) -> Union[dict, str]:
        """
        Get MIME type per attribute (or for all attributes).

        Args:
            credential_id: credential id
            attr: attribute of interest or omit for all

        Returns: Attribute MIME type or dict mapping attribute names to MIME types
            attr_meta_json = all_meta.tags.get(attr)

        """
        try:
            mime_types_record = await IndySdkStorage(self.wallet).get_record(
                IndyHolder.RECORD_TYPE_MIME_TYPES,
                f"{IndyHolder.RECORD_TYPE_MIME_TYPES}::{credential_id}",
            )
        except StorageError:
            return None  # no MIME types: not an error

        return mime_types_record.tags.get(attr) if attr else mime_types_record.tags

    async def create_presentation(
        self,
        presentation_request: dict,
        requested_credentials: dict,
        schemas: dict,
        credential_definitions: dict,
        rev_states: dict = None,
    ) -> str:
        """
        Get credentials stored in the wallet.

        Args:
            presentation_request: Valid indy format presentation request
            requested_credentials: Indy format requested credentials
            schemas: Indy formatted schemas JSON
            credential_definitions: Indy formatted credential definitions JSON
            rev_states: Indy format revocation states JSON

        """

        with IndyErrorHandler("Error when constructing proof", IndyHolderError):
            presentation_json = await indy.anoncreds.prover_create_proof(
                self.wallet.handle,
                json.dumps(presentation_request),
                json.dumps(requested_credentials),
                self.wallet.master_secret_id,
                json.dumps(schemas),
                json.dumps(credential_definitions),
                json.dumps(rev_states) if rev_states else "{}",
            )

        return presentation_json

    async def create_revocation_state(
        self,
        cred_rev_id: str,
        rev_reg_def: dict,
        rev_reg_delta: dict,
        timestamp: int,
        tails_file_path: str,
    ) -> str:
        """
        Create current revocation state for a received credential.

        Args:
            cred_rev_id: credential revocation id in revocation registry
            rev_reg_def: revocation registry definition
            rev_reg_delta: revocation delta
            timestamp: delta timestamp

        Returns:
            the revocation state

        """

        with IndyErrorHandler(
            "Error when constructing revocation state", IndyHolderError
        ):
            tails_file_reader = await create_tails_reader(tails_file_path)
            rev_state_json = await indy.anoncreds.create_revocation_state(
                tails_file_reader,
                rev_reg_def_json=json.dumps(rev_reg_def),
                cred_rev_id=cred_rev_id,
                rev_reg_delta_json=json.dumps(rev_reg_delta),
                timestamp=timestamp,
            )

        return rev_state_json
