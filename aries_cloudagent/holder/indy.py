"""Indy holder implementation."""

import json
import logging

from collections import OrderedDict
from typing import Sequence, Tuple, Union

import indy.anoncreds
from indy.error import ErrorCode, IndyError

from ..indy import create_tails_reader
from ..indy.error import IndyErrorHandler
from ..storage.indy import IndyStorage
from ..storage.error import StorageError, StorageNotFoundError
from ..storage.record import StorageRecord

from ..wallet.error import WalletNotFoundError

from .base import BaseHolder, HolderError


class IndyHolder(BaseHolder):
    """Indy holder class."""

    RECORD_TYPE_MIME_TYPES = "attribute-mime-types"

    def __init__(self, wallet):
        """
        Initialize an IndyHolder instance.

        Args:
            wallet: IndyWallet instance

        """
        self.logger = logging.getLogger(__name__)
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

        with IndyErrorHandler("Error when creating credential request", HolderError):
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

        self.logger.debug(
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
        with IndyErrorHandler("Error when storing credential in wallet", HolderError):
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
                indy_stor = IndyStorage(self.wallet)
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
        with IndyErrorHandler(
            "Error when constructing wallet credential query", HolderError
        ):
            (
                search_handle,
                record_count,
            ) = await indy.anoncreds.prover_search_credentials(
                self.wallet.handle, json.dumps(wql)
            )

        # We need to move the database cursor position manually...
        if start > 0:
            # TODO: move cursor in chunks to avoid exploding memory
            await indy.anoncreds.prover_fetch_credentials(search_handle, start)

        credentials_json = await indy.anoncreds.prover_fetch_credentials(
            search_handle, count
        )
        await indy.anoncreds.prover_close_credentials_search(search_handle)

        credentials = json.loads(credentials_json)
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

        with IndyErrorHandler(
            "Error when constructing wallet credential query", HolderError
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
                # We need to move the database cursor position manually...
                if start > 0:
                    # TODO: move cursors in chunks to avoid exploding memory
                    await indy.anoncreds.prover_fetch_credentials_for_proof_req(
                        search_handle, reft, start
                    )
                (
                    credentials_json
                ) = await indy.anoncreds.prover_fetch_credentials_for_proof_req(
                    search_handle, reft, count
                )
                credentials = json.loads(credentials_json)
                for cred in credentials:
                    cred_id = cred["cred_info"]["referent"]
                    if cred_id not in creds_dict:
                        cred["presentation_referents"] = {reft}
                        creds_dict[cred_id] = cred
                    else:
                        creds_dict[cred_id]["presentation_referents"].add(reft)

        finally:
            # Always close
            await indy.anoncreds.prover_close_credentials_search_for_proof_req(
                search_handle
            )

        for cred in creds_dict.values():
            cred["presentation_referents"] = list(cred["presentation_referents"])

        return tuple(creds_dict.values())[:count]

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
        except IndyError as e:
            if e.error_code == ErrorCode.WalletItemNotFound:
                raise WalletNotFoundError(
                    "Credential not found in the wallet: {}".format(credential_id)
                )
            else:
                raise IndyErrorHandler.wrap_error(
                    e, "Error when fetching credential", HolderError
                ) from e

        return credential_json

    async def delete_credential(self, credential_id: str):
        """
        Remove a credential stored in the wallet.

        Args:
            credential_id: Credential id to remove

        """
        try:
            indy_stor = IndyStorage(self.wallet)
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
        except IndyError as e:
            if e.error_code == ErrorCode.WalletItemNotFound:
                raise WalletNotFoundError(
                    "Credential not found in the wallet: {}".format(credential_id)
                )
            else:
                raise IndyErrorHandler.wrap_error(
                    e, "Error when deleting credential", HolderError
                ) from e

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
            mime_types_record = await IndyStorage(self.wallet).get_record(
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
        rev_states_json: dict = None,
    ) -> str:
        """
        Get credentials stored in the wallet.

        Args:
            presentation_request: Valid indy format presentation request
            requested_credentials: Indy format requested_credentials
            schemas: Indy formatted schemas_json
            credential_definitions: Indy formatted schemas_json
            rev_states_json: Indy format revocation states

        """

        with IndyErrorHandler("Error when constructing proof", HolderError):
            presentation_json = await indy.anoncreds.prover_create_proof(
                self.wallet.handle,
                json.dumps(presentation_request),
                json.dumps(requested_credentials),
                self.wallet.master_secret_id,
                json.dumps(schemas),
                json.dumps(credential_definitions),
                json.dumps(rev_states_json) if rev_states_json else "{}",
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
        Get credentials stored in the wallet.

        Args:
            cred_rev_id: credential revocation id in revocation registry
            rev_reg_def: revocation registry definition
            rev_reg_delta: revocation delta
            timestamp: delta timestamp

        Returns:
            the revocation state

        """

        tails_file_reader = await create_tails_reader(tails_file_path)
        rev_state_json = await indy.anoncreds.create_revocation_state(
            tails_file_reader,
            rev_reg_def_json=json.dumps(rev_reg_def),
            cred_rev_id=cred_rev_id,
            rev_reg_delta_json=json.dumps(rev_reg_delta),
            timestamp=timestamp,
        )

        return rev_state_json
