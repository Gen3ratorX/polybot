from __future__ import annotations

from web3 import Web3


USDC_ADDRESS_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ERC20_BALANCE_OF_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


class PolygonClient:
    def __init__(self, rpc_url: str) -> None:
        self.rpc_url = rpc_url
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))

    def is_connected(self) -> bool:
        return bool(self.w3.is_connected())

    def get_chain_id(self) -> int:
        return int(self.w3.eth.chain_id)

    def get_native_balance(self, address: str) -> float:
        checksum = Web3.to_checksum_address(address)
        raw_balance = self.w3.eth.get_balance(checksum)
        return raw_balance / 1_000_000_000_000_000_000

    def get_erc20_balance(self, token_address: str, holder_address: str, *, decimals: int) -> float:
        token = self.w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_BALANCE_OF_ABI,
        )
        raw_balance = token.functions.balanceOf(
            Web3.to_checksum_address(holder_address)
        ).call()
        return raw_balance / (10**decimals)

    def get_usdc_balance(self, holder_address: str) -> float:
        return self.get_erc20_balance(USDC_ADDRESS_POLYGON, holder_address, decimals=6)
