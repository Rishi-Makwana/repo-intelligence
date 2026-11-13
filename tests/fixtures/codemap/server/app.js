const express = require("express");
const app = express();

function listAddresses(req, res) {
  res.json([]);
}

const suggestAddress = async (req, res) => {
  res.json([]);
};

app.get("/addresses", listAddresses);
app.post("/addresses/suggest", auth, suggestAddress);
app.delete("/addresses/:id", (req, res) => res.sendStatus(204));

class AddressCache {
  lookup(key) {
    return null;
  }
}

module.exports = { app, AddressCache };
